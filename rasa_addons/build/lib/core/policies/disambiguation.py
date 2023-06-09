from __future__ import annotations

import json
import logging
import os

from typing import Any, List, Text, Dict

import rasa.shared.utils.io
import re
from rasa.core.actions.action import ACTION_LISTEN_NAME
from rasa.core.constants import FALLBACK_POLICY_PRIORITY

from rasa.shared.core.domain import Domain
from rasa.core.policies.policy import Policy, confidence_scores_for

from rasa.engine.storage.resource import Resource
from rasa.shared.core.generator import TrackerWithCachedStates

from rasa.shared.core.events import SlotSet
from rasa.shared.core.trackers import DialogueStateTracker
from rasa.shared.nlu.interpreter import NaturalLanguageInterpreter

from typing import Optional, Dict, Text, Any

from rasa.core.featurizers.tracker_featurizers import TrackerFeaturizer
from rasa.engine.graph import ExecutionContext
from rasa.engine.storage.storage import ModelStorage
logger = logging.getLogger(__name__)


class BotfrontDisambiguationPolicy(Policy):
    @staticmethod
    def _standard_featurizer():
        return None

    def __init__(
        self,
        config: Dict[Text, Any],
        model_storage: ModelStorage,
        resource: Resource,
        execution_context: ExecutionContext,
        featurizer: Optional[TrackerFeaturizer] = None,
        disambiguation_trigger: str = "$0 < 2 * $1",
        fallback_trigger: float = 0.30,
        disambiguation_template: Text = "utter_disambiguation",
        excluded_intents: List = [r"^chitchat\..*", r"^basics\..*"],
        n_suggestions: int = 2,
    ) -> None:
        super(BotfrontDisambiguationPolicy, self).__init__(
            config, model_storage, resource, execution_context, featurizer)

        self.disambiguation_trigger = disambiguation_trigger
        self.fallback_trigger = fallback_trigger
        self.fallback_default_confidence = 0.30
        self.disambiguation_action = "action_botfront_disambiguation"
        self.disambiguation_followup_action = "action_botfront_disambiguation_followup"
        self.fallback_action = "action_botfront_fallback"  # returns utter_fallback
        self.disambiguation_template = disambiguation_template
        self.excluded_intents = excluded_intents
        self.n_suggestions = n_suggestions

    @staticmethod
    def get_default_config() -> Dict[Text, Any]:
        return {"key1": "value1"}

    @classmethod
    def create(
        cls,
        config: Dict[Text, Any],
        model_storage: ModelStorage,
        resource: Resource,
        execution_context: ExecutionContext,
    ) -> BotfrontDisambiguationPolicy:
        return cls(config, model_storage, resource, execution_context)

    def train(
        self,
        training_trackers: List[DialogueStateTracker],
        domain: Domain,
        **kwargs: Any,
    ) -> Resource:
        self.persist()
        return self._resource

    def generate_disambiguation_message(self, intent_ranking, entities):
        intents = [
            (
                intent.get("name"),
                self.fill_entity(intent.get("canonical", intent.get("name")), entities),
            )
            for intent in intent_ranking
            if intent.get("name") is not None
            and len(
                [
                    excl
                    for excl in self.excluded_intents
                    if re.compile(excl).fullmatch(intent.get("name")) != None
                ]
            )
            < 1
        ][: self.n_suggestions]

        entities_json = (
            json.dumps({e.get("entity"): e.get("value") for e in entities})
            if len(entities) > 0
            else ""
        )

        quick_replies = []
        for intent in intents:
            quick_replies.append(
                {
                    "title": intent[1],
                    "type": "postback",
                    "payload": "/{}{}".format(intent[0], entities_json),
                }
            )
        return {
            "template": self.disambiguation_template,
            "quick_replies": quick_replies,
        }

    @staticmethod
    def fill_entity(template, entities):
        title = template
        for e in entities:
            title = title.replace("{" + e.get("entity") + "}", e.get("value"))
        title = re.sub(r"{.*?}", r"", title)
        return title

    @staticmethod
    def set_slot(tracker, message):
        if len(message.get("quick_replies", [])) < 2:
            return None  # abort if only deny_suggestions button would be shown
        try:
            tracker.update(SlotSet("disambiguation_message", value=message))
            result = message
        except Exception as e:
            logger.error("Could not set message slot: {}".format(e))
            result = None
        return result

    @staticmethod
    def _should_disambiguate(intent_ranking, trigger):
        import re

        # pattern to match $0, $1, $2, ... and returning 0, 1, 2,... in match groups
        pattern = re.compile(r"\$(\d)")
        eval_string = trigger
        # matches: an array of intents indices to consider in intent_ranking
        matches = re.findall(pattern, trigger)
        for i in matches:
            # if not enough intents in ranking to apply the rule, policy rule can't be triggered
            if int(i) >= len(intent_ranking):
                return False
            eval_string = re.sub(
                r"\$" + i, str(intent_ranking[int(i)].get("confidence", 1)), eval_string
            )

        return eval(eval_string, {"__builtins__": {}})

    @staticmethod
    def _should_fallback(intent_ranking, trigger):
        bonified_ranking = intent_ranking + [{"confidence": 0}]
        return bonified_ranking[0].get("confidence", 1) < trigger

    def _is_user_input_expected(self, tracker: DialogueStateTracker) -> bool:
        return tracker.latest_action_name in [
            self.fallback_action,
            self.disambiguation_action,
        ]

    def _have_options_been_suggested(self, tracker: DialogueStateTracker) -> bool:
        return tracker.last_executed_action_has(self.disambiguation_action)

    def predict_action_probabilities(
        self,
        tracker: DialogueStateTracker,
        domain: Domain,
        interpreter: NaturalLanguageInterpreter,
        rule_only_data: Optional[Dict[Text, Any]] = None,
        precomputations: Optional[MessageContainerForCoreFeaturization] = None,
    ) -> List[float]:

        parse_data = tracker.latest_message.parse_data
        entities = parse_data.get("entities", [])
        intent_ranking = parse_data.get("intent_ranking", [])
        if len(intent_ranking) == 0 and parse_data.get("intent") is not None:
            intent_ranking = [parse_data.get("intent")]
        can_apply = tracker.latest_action_name == ACTION_LISTEN_NAME
        should_fallback = can_apply and self._should_fallback(
            intent_ranking, self.fallback_trigger
        )
        should_disambiguate = can_apply and self._should_disambiguate(
            intent_ranking, self.disambiguation_trigger
        )

        if self._is_user_input_expected(tracker):
            # Shut up and listen
            result = confidence_scores_for(ACTION_LISTEN_NAME, 1.0, domain)

        elif should_fallback:
            logger.debug("Triggering fallback")
            result = confidence_scores_for(self.fallback_action, 1.0, domain)

        elif self._have_options_been_suggested(tracker):
            if not should_disambiguate:
                logger.debug("Successfully disambiguated")
                result = confidence_scores_for(
                    self.disambiguation_followup_action, 1.0, domain
                )
            else:
                logger.debug(
                    "Will not disambiguate a second time so fast -- triggering fallback"
                )
                result = confidence_scores_for(self.fallback_action, 1.0, domain)

        elif should_disambiguate:
            logger.debug("Triggering disambiguation")
            disambiguation_message = self.generate_disambiguation_message(
                intent_ranking, entities,
            )
            slot_set = self.set_slot(tracker, disambiguation_message)
            if slot_set:
                result = confidence_scores_for(self.disambiguation_action, 1.0, domain)
            else:
                result = confidence_scores_for(self.fallback_action, 1.0, domain)

        else:
            # Nothing to see here; setting fallback to default confidence
            result = confidence_scores_for(
                self.fallback_action, self.fallback_default_confidence, domain
            )

        return result

    def persist(self) -> None:
        """Persists the policy to storage."""
        with self._model_storage.write_to(self._resource) as directory:
            if self.featurizer is not None:
                self.featurizer.persist(directory)

            config_file = os.path.join(directory, "botfront_disambiguation_policy.json")
            meta = {
                "priority": self.priority,
                "disambiguation_trigger": self.disambiguation_trigger,
                "fallback_trigger": self.fallback_trigger,
                "disambiguation_template": self.disambiguation_template,
                "excluded_intents": self.excluded_intents,
                "n_suggestions": self.n_suggestions,
            }
            rasa.shared.utils.io.dump_obj_as_json_to_file(config_file,
                                                          self.get_model_data())

    @classmethod
    def load(cls,
             config: Dict[Text, Any],
             model_storage: ModelStorage,
             resource: Resource,
             execution_context: ExecutionContext,
             **kwargs: Any) -> BotfrontDisambiguationPolicy:
        featurizer = None
        model_data = {}
        meta = {}

        try:
            with model_storage.read_from(resource) as path:
                meta_path = os.path.join(path, "botfront_disambiguation_policy.json")
                if (Path(path) / FEATURIZER_FILE).is_file():
                    featurizer = TrackerFeaturizer.load(path)
                if meta_path.is_file():
                    meta = json.loads(rasa.shared.utils.io.read_file(meta_path))

        except (ValueError, FileNotFoundError, FileIOException):
            logger.debug(
                f"Couldn't load metadata for policy '{cls.__name__}' as the persisted "
                f"metadata couldn't be loaded."
            )

        return cls(
            config, model_storage, resource, execution_context,
            featurizer=featurizer, model_data=meta
        )
