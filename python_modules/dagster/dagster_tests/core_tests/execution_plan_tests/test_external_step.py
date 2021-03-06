import uuid

import pytest

from dagster import (
    Field,
    ModeDefinition,
    RetryRequested,
    execute_pipeline,
    pipeline,
    reconstructable,
    resource,
    seven,
    solid,
)
from dagster.core.definitions.no_step_launcher import no_step_launcher
from dagster.core.events import DagsterEventType
from dagster.core.execution.api import create_execution_plan
from dagster.core.execution.context_creation_pipeline import pipeline_initialization_manager
from dagster.core.execution.plan.external_step import (
    LocalExternalStepLauncher,
    local_external_step_launcher,
    step_context_to_step_run_ref,
    step_run_ref_to_step_context,
)
from dagster.core.execution.retries import Retries, RetryMode
from dagster.core.instance import DagsterInstance
from dagster.core.storage.pipeline_run import PipelineRun
from dagster.utils.merger import deep_merge_dicts

ENVIRONMENT_DICT_BASE = {'solids': {'return_two': {'config': {'a': 'b'}}}}


def make_environment_dict(scratch_dir, mode):
    if mode in ['external', 'request_retry']:
        step_launcher_resource_keys = ['first_step_launcher', 'second_step_launcher']
    else:
        step_launcher_resource_keys = ['second_step_launcher']
    return deep_merge_dicts(
        ENVIRONMENT_DICT_BASE,
        {
            'resources': {
                step_launcher_resource_key: {'config': {'scratch_dir': scratch_dir}}
                for step_launcher_resource_key in step_launcher_resource_keys
            },
            'storage': {'filesystem': {'config': {'base_dir': scratch_dir}}},
        },
    )


class RequestRetryLocalExternalStepLauncher(LocalExternalStepLauncher):
    def launch_step(self, step_context, prior_attempts_count):
        if prior_attempts_count == 0:
            raise RetryRequested()
        else:
            return super(RequestRetryLocalExternalStepLauncher, self).launch_step(
                step_context, prior_attempts_count
            )


@resource(config=local_external_step_launcher.config_field)
def request_retry_local_external_step_launcher(context):
    return RequestRetryLocalExternalStepLauncher(**context.resource_config)


def define_basic_pipeline():
    @solid(required_resource_keys=set(['first_step_launcher']), config_schema={'a': Field(str)})
    def return_two(_):
        return 2

    @solid(required_resource_keys=set(['second_step_launcher']))
    def add_one(_, num):
        return num + 1

    @pipeline(
        mode_defs=[
            ModeDefinition(
                'external',
                resource_defs={
                    'first_step_launcher': local_external_step_launcher,
                    'second_step_launcher': local_external_step_launcher,
                },
            ),
            ModeDefinition(
                'internal_and_external',
                resource_defs={
                    'first_step_launcher': no_step_launcher,
                    'second_step_launcher': local_external_step_launcher,
                },
            ),
            ModeDefinition(
                'request_retry',
                resource_defs={
                    'first_step_launcher': request_retry_local_external_step_launcher,
                    'second_step_launcher': request_retry_local_external_step_launcher,
                },
            ),
        ]
    )
    def basic_pipeline():
        add_one(return_two())

    return basic_pipeline


def initialize_step_context(scratch_dir):
    pipeline_run = PipelineRun(
        pipeline_name='foo_pipeline',
        run_id=str(uuid.uuid4()),
        environment_dict=make_environment_dict(scratch_dir, 'external'),
        mode='external',
    )

    plan = create_execution_plan(
        reconstructable(define_basic_pipeline), pipeline_run.environment_dict, mode='external'
    )

    initialization_manager = pipeline_initialization_manager(
        plan, pipeline_run.environment_dict, pipeline_run, DagsterInstance.ephemeral(),
    )
    for _ in initialization_manager.generate_setup_events():
        pass
    pipeline_context = initialization_manager.get_object()

    active_execution = plan.start(retries=Retries(RetryMode.DISABLED))
    step = active_execution.get_next_step()
    step_context = pipeline_context.for_step(step)
    return step_context


def test_step_context_to_step_run_ref():
    step_context = initialize_step_context('')
    step = step_context.step
    step_run_ref = step_context_to_step_run_ref(step_context, 0)
    assert step_run_ref.environment_dict == step_context.pipeline_run.environment_dict
    assert step_run_ref.run_id == step_context.pipeline_run.run_id

    rehydrated_step_context = step_run_ref_to_step_context(step_run_ref)
    assert rehydrated_step_context.required_resource_keys == step_context.required_resource_keys
    rehydrated_step = rehydrated_step_context.step
    assert rehydrated_step.pipeline_name == step.pipeline_name
    assert rehydrated_step.key_suffix == step.key_suffix
    assert rehydrated_step.step_inputs == step.step_inputs
    assert rehydrated_step.step_outputs == step.step_outputs
    assert rehydrated_step.kind == step.kind
    assert rehydrated_step.solid_handle.name == step.solid_handle.name
    assert rehydrated_step.logging_tags == step.logging_tags
    assert rehydrated_step.tags == step.tags


def test_local_external_step_launcher():
    with seven.TemporaryDirectory() as tmpdir:
        step_context = initialize_step_context(tmpdir)

        step_launcher = LocalExternalStepLauncher(tmpdir)
        events = list(step_launcher.launch_step(step_context, 0))
        event_types = [event.event_type for event in events]
        assert DagsterEventType.STEP_START in event_types
        assert DagsterEventType.STEP_SUCCESS in event_types
        assert DagsterEventType.STEP_FAILURE not in event_types


@pytest.mark.parametrize('mode', ['external', 'internal_and_external'])
def test_pipeline(mode):
    with seven.TemporaryDirectory() as tmpdir:
        result = execute_pipeline(
            pipeline=reconstructable(define_basic_pipeline),
            mode=mode,
            environment_dict=make_environment_dict(tmpdir, mode),
        )
        assert result.result_for_solid('return_two').output_value() == 2
        assert result.result_for_solid('add_one').output_value() == 3


def test_launcher_requests_retry():
    mode = 'request_retry'
    with seven.TemporaryDirectory() as tmpdir:
        result = execute_pipeline(
            pipeline=reconstructable(define_basic_pipeline),
            mode=mode,
            environment_dict=make_environment_dict(tmpdir, mode),
        )
        assert result.result_for_solid('return_two').output_value() == 2
        assert result.result_for_solid('add_one').output_value() == 3
        for step_key, events in result.events_by_step_key.items():
            if step_key:
                event_types = [event.event_type for event in events]
                assert DagsterEventType.STEP_UP_FOR_RETRY in event_types
                assert DagsterEventType.STEP_RESTARTED in event_types
