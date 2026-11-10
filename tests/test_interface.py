from dbarena.actions.registry import ACTION_NAMES
from dbarena.env.interface import action_specs, observation_space_catalog
from dbarena.models.observations import Observation, ObservationType


class TestActionSpecs:
    def test_specs_cover_exactly_the_registered_actions(self):
        specs = action_specs()
        assert [s["tool"] for s in specs] == list(ACTION_NAMES)

    def test_every_spec_is_descriptive(self):
        for spec in action_specs():
            assert isinstance(spec["summary"], str) and len(spec["summary"]) > 20
            assert isinstance(spec["arguments"], dict)
            assert all(isinstance(k, str) and isinstance(v, str) for k, v in spec["arguments"].items())
            example = spec["example"]
            assert example["tool"] == spec["tool"]
            assert isinstance(example["arguments"], dict)

    def test_submit_spec_documents_tuples(self):
        submit = next(s for s in action_specs() if s["tool"] == "submit_final_answer")
        assert "tuples" in submit["arguments"]

    def test_submit_spec_describes_confirmation_and_state_checks(self):
        submit = next(s for s in action_specs() if s["tool"] == "submit_final_answer")
        assert "confirmation" in submit["summary"]
        assert "state checks" in submit["summary"]


class TestObservationSpaceCatalog:
    def test_catalog_covers_all_five_types(self):
        entries = observation_space_catalog()
        assert [e["type"] for e in entries] == [t.value for t in ObservationType]
        for entry in entries:
            assert isinstance(entry["description"], str) and entry["description"]

    def test_every_example_is_a_valid_envelope(self):
        for entry in observation_space_catalog():
            restored = Observation.from_dict(entry["example"])
            assert restored.type.value == entry["type"]
            assert restored.to_dict() == entry["example"]

    def test_examples_carry_the_producing_action(self):
        for entry in observation_space_catalog():
            example = entry["example"]
            if entry["type"] == "schema_description":
                continue
            assert example["action"] is not None, entry["type"]
