"""Current command admission, parameter calls, durable queues, and recovery."""

import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from engine.actions import load_actions
from engine.commands import load_commands
from engine.core import acknowledgment
from engine.errors import CorruptLogError, EngineError
from engine.runner import action_configuration
from engine.tests.support import EngineFixture, install_approval_handler
from engine.tests import test_runner


class CommandTests(EngineFixture):
    wait_for = test_runner.RunnerTests.wait_for
    finished = test_runner.RunnerTests.finished
    clean_workers = test_runner.RunnerTests.clean_workers

    def setUp(self):
        super().setUp()
        self.addCleanup(self.clean_workers)
        install_approval_handler(self)
        self.set_actions({
            'prepare': {'handler': 'action_command'},
            'approve': {'handler': 'action_test_approval'},
            'process': {'handler': 'action_command'},
        })
        self.definitions = {
            'one-action': {'handler': 'command_one_action'},
            'chain': {'handler': 'command_one_shot', 'actions': [
                {'action': 'prepare', 'dependencies': [], 'parameter_bindings': {}},
                {'action': 'approve', 'dependencies': ['prepare'], 'parameter_bindings': {'source': {'step': 'prepare', 'value': 'result'}}},
                {'action': 'process', 'dependencies': ['approve'], 'parameter_bindings': {'source': {'step': 'approve', 'value': 'result'}}},
            ]},
        }

    def engine(self, **kwargs):
        return self.make_engine(commands=self.definitions, **kwargs)

    def submit(self, engine, command='chain', request_id='chain-request', parameters=None):
        return engine.command('set-one', 'task-one', {'request_id': request_id, 'command': command, 'parameters': parameters or {}})

    def read_pool(self):
        return json.loads((self.review / 'action-pool.json').read_text())

    def test_chain_binds_parameters_and_dispatches_each_step_to_worker(self):
        engine = self.engine()
        receipt = self.submit(engine, parameters={'action_parameters': {'approve': {'source': {'fake': True}}}})
        ids = receipt['action_ids']
        self.assertEqual(receipt['type'], 'command_started')
        self.assertEqual([item['action'] for item in self.read_pool()], ['approve', 'process'])
        state = engine.state('set-one', 'task-one')
        self.assertEqual([state['actions'][identity]['status'] for identity in ids], ['started', 'queued', 'queued'])
        self.assertNotIn('dependencies', state['actions'][ids[0]])
        self.finish(engine, ids[0], result={'value': 42})
        approval = engine.state('set-one', 'task-one')['actions'][ids[1]]
        config = action_configuration(engine, ('set-one', 'task-one'), approval)
        self.assertEqual(config['action']['parameters'], {'source': {'value': 42}})
        self.assertNotIn('dependency_results', config)
        self.assertEqual(approval['status'], 'started')
        self.assertEqual([item['action'] for item in self.read_pool()], ['process'])
        self.finish(engine, ids[1], result={'approved': True})
        state = engine.state('set-one', 'task-one')
        self.assertEqual(state['actions'][ids[2]]['parameters'], {'source': {'approved': True}})
        self.assertEqual(self.read_pool(), [])
        self.finish(engine, ids[2], result={'done': True})
        events = engine.events('set-one', 'task-one')['events']
        self.assertEqual([e['type'] for e in events], ['command_started', 'started', 'completed', 'started', 'completed', 'started', 'completed', 'command_completed'])
        self.assertEqual([e['type'] for e in events if e['action_id'] == ids[1]], ['started', 'completed'])
        self.assertTrue(all('mode' not in step for step in events[0]['payload']['steps']))
        self.assertTrue(all('mode' not in action for action in engine.state('set-one', 'task-one')['actions'].values()))
        command = engine.state('set-one', 'task-one')['commands'][receipt['command_id']]
        self.assertEqual(command['outcome'], 'succeeded')
        self.assertEqual(command['result']['actions'][-1]['result'], {'done': True})
        self.assertEqual(self.submit(engine, parameters={'action_parameters': {'approve': {'source': {'fake': True}}}}), receipt)

    def test_failure_cancels_pool_with_original_cause(self):
        engine = self.engine()
        receipt = self.submit(engine)
        self.finish(engine, receipt['action_ids'][0], outcome='failed', error='original diagnostic')
        state = engine.state('set-one', 'task-one')
        command = state['commands'][receipt['command_id']]
        self.assertEqual((command['outcome'], command['error'], command['failed_action_id']), ('failed', 'original diagnostic', receipt['action_ids'][0]))
        self.assertEqual(self.read_pool(), [])
        for identity in receipt['action_ids'][1:]:
            self.assertEqual((state['actions'][identity]['outcome'], state['actions'][identity]['error']), ('cancelled', 'original diagnostic'))
        cancellations = [e for e in engine.events('set-one', 'task-one')['events'] if e['type'] == 'cancelled']
        self.assertEqual(len(cancellations), 2)
        self.assertTrue(all(e['payload']['failed_action_id'] == receipt['action_ids'][0] for e in cancellations))
        engine._runner.initiate.assert_called_once()
        engine.close()
        restored = self.engine()
        self.assertEqual(restored.state('set-one', 'task-one'), state)

    def test_historical_command_plans_replay_worker_and_direct_completions(self):
        engine = self.engine()
        receipt = self.submit(engine)
        for identity, result in zip(receipt['action_ids'], ({'value': 42}, {'approved': True}, {'done': True})):
            self.finish(engine, identity, result=result)
        engine.close()
        log = self.review / 'events.jsonl'
        original = [json.loads(line) for line in log.read_text().splitlines()]
        for inline in (False, True):
            with self.subTest(inline=inline):
                history = copy.deepcopy(original)
                for step in history[0]['payload']['steps']:
                    step['mode'] = 'async'
                if inline:
                    history[0]['payload']['steps'][1]['mode'] = 'sync'
                    history = [event for event in history if not (
                        event['type'] == 'started' and event['action_id'] == receipt['action_ids'][1]
                    )]
                for sequence, event in enumerate(history, 1):
                    event['sequence'] = sequence
                log.write_text(''.join(json.dumps(event) + '\n' for event in history))
                restored = self.engine()
                state = restored.state('set-one', 'task-one')
                self.assertEqual(state['commands'][receipt['command_id']]['outcome'], 'succeeded')
                self.assertEqual(state['actions'][receipt['action_ids'][1]]['result'], {'approved': True})
                self.assertTrue(all('mode' not in action for action in state['actions'].values()))
                self.assertEqual(self.submit(restored), receipt)
                restored._runner.initiate.assert_not_called()
                restored.close()

    def test_worker_failure_and_launch_failure_abort_command(self):
        engine = self.engine()
        receipt = self.submit(engine)
        self.finish(engine, receipt['action_ids'][0], result={})
        self.finish(engine, receipt['action_ids'][1], outcome='failed', error='commit failed')
        state = engine.state('set-one', 'task-one')
        self.assertEqual(state['commands'][receipt['command_id']]['error'], 'commit failed')
        self.assertEqual(state['actions'][receipt['action_ids'][2]]['outcome'], 'cancelled')
        with patch.object(engine._runner, 'initiate', side_effect=OSError('cannot launch')), self.assertLogs('engine.core', level='ERROR'):
            other = self.submit(engine, request_id='launch-failed')
        self.assertIn('cannot launch', engine.state('set-one', 'task-one')['commands'][other['command_id']]['error'])
        self.assertEqual(self.read_pool(), [])

    def test_one_action_requires_explicit_parameters_and_never_looks_up_history(self):
        engine = self.engine()
        with self.assertRaisesRegex(EngineError, 'requires source'):
            self.submit(engine, 'one-action', 'missing', {'action': 'approve'})
        self.assertEqual(engine.state('set-one', 'task-one')['revision'], 0)
        source = {'caller': 'supplied'}
        receipt = self.submit(engine, 'one-action', 'debug', {'action': 'approve', 'action_parameters': {'source': source}})
        action = engine.state('set-one', 'task-one')['actions'][receipt['action_id']]
        self.assertEqual(action['parameters'], {'source': source})
        self.assertEqual(action['status'], 'started')
        self.assertEqual(len(receipt['action_ids']), 1)
        self.finish(engine, receipt['action_id'], result={'approved': True})
        self.assertEqual(engine.state('set-one', 'task-one')['commands'][receipt['command_id']]['outcome'], 'succeeded')

    def test_request_rejects_inputs_runner_and_unknown_parameters(self):
        engine = self.engine()
        for parameters in (
            {'action': 'prepare', 'inputs': {}},
            {'action': 'prepare', 'runner': {'argv': ['false']}},
            {'action': 'approve', 'action_parameters': {'source': {}, 'runner': {}}},
            {'action': 'approve', 'action_parameters': {'source': []}},
            {'action': 'prepare', 'action_parameters': []},
            {'action': []},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(EngineError):
                self.submit(engine, 'one-action', 'invalid', parameters)
        self.assertEqual(engine.state('set-one', 'task-one')['revision'], 0)

    def test_runner_settings_are_internal_and_captured(self):
        self.set_actions({'prepare': {'handler': 'action_command', 'runner': {'argv': [sys.executable, '-c', 'print("internal")']}}})
        engine = self.engine()
        receipt = self.submit(engine, 'one-action', parameters={'action': 'prepare', 'action_parameters': {'value': 'literal'}})
        action = engine.state('set-one', 'task-one')['actions'][receipt['action_id']]
        self.assertEqual(action['execution']['argv'][-1], 'print("internal")')
        self.assertEqual(action['parameters'], {'value': 'literal'})

    def test_task_is_exclusive_and_concurrent_retries_accept_once(self):
        engine = self.engine()
        with ThreadPoolExecutor(max_workers=8) as pool:
            receipts = list(pool.map(lambda _: self.submit(engine), range(16)))
        self.assertTrue(all(r == receipts[0] for r in receipts))
        self.assertEqual(engine.state('set-one', 'task-one')['revision'], 2)
        for body in (
            {'request_id': 'second', 'command': 'chain'},
            {'request_id': 'debug', 'command': 'one-action', 'parameters': {'action': 'prepare'}},
            {'request_id': 'legacy', 'action': 'prepare'},
        ):
            with self.assertRaises(EngineError) as caught:
                engine.command('set-one', 'task-one', body)
            self.assertEqual(caught.exception.code, 'command_already_active')
        with self.assertRaises(EngineError) as caught:
            self.submit(engine, parameters={'value': 'different'})
        self.assertEqual(caught.exception.code, 'request_id_conflict')

    def test_cancel_by_queued_action_cancels_entire_command(self):
        engine = self.engine()
        receipt = self.submit(engine)
        request = {'request_id': 'cancel', 'action_id': receipt['action_ids'][2]}
        cancelled = engine.cancel('set-one', 'task-one', request)
        self.assertEqual(engine.cancel('set-one', 'task-one', request), cancelled)
        self.assertEqual(self.read_pool(), [])
        self.assertTrue(engine.state('set-one', 'task-one')['actions'][receipt['action_ids'][0]]['cancel_requested'])
        self.finish(engine, receipt['action_ids'][0], outcome='cancelled', error='Action cancelled.')
        self.assertEqual(engine.state('set-one', 'task-one')['commands'][receipt['command_id']]['outcome'], 'cancelled')
        engine._runner.initiate.assert_called_once()

    def test_cancel_prevents_next_step_even_when_worker_reports_success(self):
        engine = self.engine()
        receipt = self.submit(engine)
        engine.cancel('set-one', 'task-one', {'request_id': 'cancel', 'command_id': receipt['command_id']})
        self.finish(engine, receipt['action_ids'][0], result={'late': 'success'})
        self.assertEqual(engine.state('set-one', 'task-one')['commands'][receipt['command_id']]['outcome'], 'cancelled')
        engine._runner.initiate.assert_called_once()

    def test_pool_and_snapshot_rebuild_from_events(self):
        engine = self.engine()
        receipt = self.submit(engine)
        state = engine.state('set-one', 'task-one')
        engine.close()
        (self.review / 'job.json').write_text('broken')
        (self.review / 'action-pool.json').write_text('[{"incorrect":true}]')
        restored = self.engine()
        self.assertEqual(restored.state('set-one', 'task-one'), state)
        self.assertEqual(self.read_pool(), state['action_pool'])
        self.assertEqual(self.submit(restored), receipt)
        restored.close()
        (self.review / 'action-pool.json').unlink()
        self.engine()
        self.assertEqual(self.read_pool(), state['action_pool'])

    def test_crash_before_first_dispatch_recovers_frozen_plan(self):
        engine = self.engine()
        with patch.object(engine._scheduler, 'advance', side_effect=OSError('crash before dispatch')):
            with self.assertRaises(OSError):
                self.submit(engine)
        receipt = acknowledgment(('set-one', 'task-one'), engine._events[('set-one', 'task-one')][0])
        engine.close()
        self.set_actions({'unrelated': {'handler': 'action_command'}})
        restored = self.engine()
        self.assertEqual(self.submit(restored), receipt)
        self.assertEqual(restored.state('set-one', 'task-one')['actions'][receipt['action_ids'][0]]['status'], 'started')
        self.assertEqual([item['action'] for item in self.read_pool()], ['approve', 'process'])

    def test_crash_between_completion_and_next_start_resumes_once(self):
        engine = self.engine()
        receipt = self.submit(engine)
        append = engine._store.append

        def fail_next(key, event):
            if event['type'] == 'started' and event['action_id'] == receipt['action_ids'][1]:
                raise OSError('next start was not written')
            append(key, event)

        with patch.object(engine._store, 'append', side_effect=fail_next), self.assertLogs('engine.core', level='ERROR'):
            with self.assertRaises(EngineError):
                self.finish(engine, receipt['action_ids'][0], result={'value': 1})
        engine.close()
        restored = self.engine()
        restored._runner.initiate.assert_called_once()
        state = restored.state('set-one', 'task-one')
        self.assertEqual(state['actions'][receipt['action_ids'][1]]['status'], 'started')
        self.assertEqual(restored._runner.initiate.call_args.args[2]['action_id'], receipt['action_ids'][1])
        self.finish(restored, receipt['action_ids'][1], result={'approved': True})
        self.finish(restored, receipt['action_ids'][2], result={})
        self.assertEqual(restored.state('set-one', 'task-one')['commands'][receipt['command_id']]['outcome'], 'succeeded')

    def test_restart_finishes_failure_cleanup_and_command_completion(self):
        engine = self.engine()
        receipt = self.submit(engine)
        with patch.object(engine._scheduler, 'advance', side_effect=OSError('crash')):
            with self.assertRaises(OSError):
                self.finish(engine, receipt['action_ids'][0], outcome='failed', error='durable failure')
        engine.close()
        restored = self.engine()
        command = restored.state('set-one', 'task-one')['commands'][receipt['command_id']]
        self.assertEqual((command['outcome'], command['error']), ('failed', 'durable failure'))
        self.assertEqual(self.read_pool(), [])

    def test_uncertain_append_retries_original_receipt_after_replay(self):
        engine = self.engine()
        append = engine._store.append

        def uncertain(key, event):
            append(key, event)
            raise OSError('fsync uncertain')

        with patch.object(engine._store, 'append', side_effect=uncertain), self.assertLogs('engine.core', level='ERROR'):
            with self.assertRaises(EngineError):
                self.submit(engine)
        engine.close()
        restored = self.engine()
        receipt = self.submit(restored)
        self.assertEqual(receipt['sequence'], 1)
        self.assertEqual(restored.state('set-one', 'task-one')['revision'], 2)

    def test_projection_failure_preserves_durable_command(self):
        engine = self.engine()
        with patch.object(engine._store, 'write_pool', side_effect=OSError('projection unavailable')), self.assertLogs('engine.core', level='ERROR'):
            receipt = self.submit(engine)
        engine.close()
        restored = self.engine()
        self.assertEqual(self.submit(restored), receipt)
        self.assertEqual(self.read_pool(), restored.state('set-one', 'task-one')['action_pool'])

    def test_new_action_definitions_reject_dependencies(self):
        config = json.loads(self.config.read_text())
        config['actions']['prepare']['dependencies'] = []
        self.config.write_text(json.dumps(config))
        with self.assertRaisesRegex(EngineError, 'no workflow dependencies'):
            load_actions(self.config)

    def test_command_definitions_reject_invalid_graphs_and_debug_bindings(self):
        for mutation in ('cycle', 'duplicate', 'unknown_binding', 'debug_options'):
            definitions = copy.deepcopy(self.definitions)
            if mutation == 'cycle':
                definitions['chain']['actions'][0]['dependencies'] = ['process']
            elif mutation == 'duplicate':
                definitions['chain']['actions'][1]['action'] = 'prepare'
            elif mutation == 'unknown_binding':
                definitions['chain']['actions'][1]['parameter_bindings']['source']['step'] = 'missing'
            else:
                definitions['one-action']['parameter_bindings'] = {}
            with self.subTest(mutation=mutation), self.assertRaises(EngineError):
                load_commands(definitions)

    def test_committed_corruption_stops_replay(self):
        engine = self.engine()
        self.submit(engine)
        engine.close()
        history = [json.loads(line) for line in (self.review / 'events.jsonl').read_text().splitlines()]
        history[1]['payload']['parameters'] = {'tampered': True}
        (self.review / 'events.jsonl').write_text(''.join(json.dumps(event) + '\n' for event in history))
        with self.assertRaises(CorruptLogError):
            self.engine()

    def test_real_worker_timeout_fails_chain_and_cleans_pool(self):
        self.set_actions({'prepare': {'handler': 'action_command', 'runner': {'argv': [sys.executable, '-c', 'import time; time.sleep(30)'], 'timeout_ms': 100, 'termination_grace_ms': 100}}, 'approve': {'handler': 'action_test_approval'}, 'process': {'handler': 'action_command'}})
        engine = self.engine(workers=True)
        receipt = self.submit(engine)
        self.assertEqual(self.finished(engine, receipt['action_ids'][0])['outcome'], 'timed_out')
        self.wait_for(lambda: engine.state('set-one', 'task-one')['commands'][receipt['command_id']]['status'] == 'completed')
        command = engine.state('set-one', 'task-one')['commands'][receipt['command_id']]
        self.assertEqual(command['outcome'], 'failed')
        self.assertEqual(command['failed_action_id'], receipt['action_ids'][0])
        self.assertEqual(self.read_pool(), [])

    def test_real_worker_restart_consumes_saved_result_without_relaunch(self):
        gate = self.base / 'release'
        count = self.base / 'count'
        text = f'from pathlib import Path; import time; Path({str(count)!r}).open("a").write("launch\\n");\nwhile not Path({str(gate)!r}).exists(): time.sleep(.05)\nprint("done")'
        self.set_actions({'prepare': {'handler': 'action_command', 'runner': {'argv': [sys.executable, '-c', text]}}})
        engine = self.engine(workers=True)
        receipt = self.submit(engine, 'one-action', parameters={'action': 'prepare'})
        self.wait_for(count.exists)
        engine.close()
        live = self.engine(workers=True)
        self.assertEqual(self.submit(live, 'one-action', parameters={'action': 'prepare'}), receipt)
        live.close()
        gate.touch()
        directory = self.review / 'jobs' / 'engine' / receipt['action_id']
        self.wait_for(lambda: (directory / 'result.json').exists())
        restored = self.engine(workers=True)
        command = restored.state('set-one', 'task-one')['commands'][receipt['command_id']]
        self.assertEqual(command['outcome'], 'succeeded')
        self.assertEqual(count.read_text(), 'launch\n')
        self.assertEqual([event['type'] for event in restored.events('set-one', 'task-one')['events']], ['command_started', 'started', 'completed', 'command_completed'])

    def test_partial_failure_cleanup_replays_each_cancellation_once(self):
        engine = self.engine()
        receipt = self.submit(engine)
        append = engine._store.append

        def interrupt_cleanup(key, event):
            if event['type'] == 'cancelled' and event['action_id'] == receipt['action_ids'][2]:
                raise OSError('crash during queue cleanup')
            append(key, event)

        with patch.object(engine._store, 'append', side_effect=interrupt_cleanup), self.assertLogs('engine.core', level='ERROR'):
            with self.assertRaises(EngineError):
                self.finish(engine, receipt['action_ids'][0], outcome='failed', error='first failure')
        engine.close()
        restored = self.engine()
        command = restored.state('set-one', 'task-one')['commands'][receipt['command_id']]
        self.assertEqual((command['outcome'], command['error']), ('failed', 'first failure'))
        events = restored.events('set-one', 'task-one')['events']
        for identity in receipt['action_ids'][1:]:
            self.assertEqual(sum(e['type'] == 'cancelled' and e['action_id'] == identity for e in events), 1)
        self.assertEqual(self.read_pool(), [])

    def test_restart_after_final_action_finishes_command_without_reexecution(self):
        engine = self.engine()
        receipt = self.submit(engine, 'one-action', parameters={'action': 'prepare'})
        with patch.object(engine._scheduler, '_finish', side_effect=OSError('crash before command completion')):
            with self.assertRaises(OSError):
                self.finish(engine, receipt['action_id'], result={'done': True})
        engine.close()
        restored = self.engine()
        self.assertEqual(restored.state('set-one', 'task-one')['commands'][receipt['command_id']]['outcome'], 'succeeded')
        restored._runner.initiate.assert_not_called()

    def test_command_provenance_makes_transitive_results_stale(self):
        engine = self.engine()
        receipt = self.submit(engine)
        self.finish(engine, receipt['action_ids'][0], result={'version': 1})
        self.finish(engine, receipt['action_ids'][1], result={'approved': True})
        self.finish(engine, receipt['action_ids'][2], result={'done': True})
        replacement = self.submit(engine, 'one-action', 'replacement', {'action': 'prepare'})
        self.finish(engine, replacement['action_id'], result={'version': 2})
        state = engine.state('set-one', 'task-one')
        self.assertTrue(state['actions'][receipt['action_ids'][1]]['stale'])
        self.assertTrue(state['actions'][receipt['action_ids'][2]]['stale'])
        self.assertFalse(state['actions'][replacement['action_id']]['stale'])

    def test_independent_tasks_run_commands_concurrently(self):
        gate = self.base / 'release'
        text = f'from pathlib import Path; import time;\nwhile not Path({str(gate)!r}).exists(): time.sleep(.05)'
        self.set_actions({'prepare': {'handler': 'action_command', 'runner': {'argv': [sys.executable, '-c', text]}}})
        other = self.task_directory.with_name('task-two')
        other.mkdir()
        (other / 'design.md').write_text('# Second task')
        engine = self.engine(workers=True)
        first = self.submit(engine, 'one-action', parameters={'action': 'prepare'})
        second = engine.command('set-one', 'task-two', {'request_id': 'second', 'command': 'one-action', 'parameters': {'action': 'prepare'}})
        self.assertEqual(engine.state('set-one', 'task-one')['commands'][first['command_id']]['status'], 'started')
        self.assertEqual(engine.state('set-one', 'task-two')['commands'][second['command_id']]['status'], 'started')
        gate.touch()
        self.finished(engine, first['action_id'])
        self.wait_for(lambda: engine.state('set-one', 'task-two')['commands'][second['command_id']]['status'] == 'completed')
        self.assertEqual(engine.state('set-one', 'task-two')['commands'][second['command_id']]['outcome'], 'succeeded')

    def test_legacy_request_is_a_command_wrapper_and_replays_its_ack(self):
        engine = self.engine()
        body = {'request_id': 'legacy-body', 'action': 'prepare', 'parameters': {'value': 12}}
        receipt = engine.command('set-one', 'task-one', body)
        self.assertEqual(receipt['type'], 'command_started')
        self.finish(engine, receipt['action_id'], result={})
        engine.close()
        restored = self.engine()
        self.assertEqual(restored.command('set-one', 'task-one', body), receipt)
        self.assertEqual(restored.state('set-one', 'task-one')['actions'][receipt['action_id']]['parameters'], {'value': 12})

    def test_replay_rejects_json_type_changes_and_modified_command_results(self):
        engine = self.engine()
        receipt = self.submit(engine, 'one-action', parameters={'action': 'prepare', 'action_parameters': {'value': 1}})
        self.finish(engine, receipt['action_id'], result={'value': 1})
        engine.close()
        log = self.review / 'events.jsonl'
        original = [json.loads(line) for line in log.read_text().splitlines()]
        for kind in ('started', 'completed', 'command_completed'):
            history = copy.deepcopy(original)
            event = next(event for event in history if event['type'] == kind)
            if kind == 'started':
                event['payload']['parameters']['value'] = True
            elif kind == 'completed':
                event['payload']['result']['value'] = True
            else:
                event['payload']['result']['actions'][0]['result']['value'] = True
            log.write_text(''.join(json.dumps(event) + '\n' for event in history))
            with self.subTest(kind=kind), self.assertRaises(CorruptLogError):
                self.engine()
        log.write_text(''.join(json.dumps(event) + '\n' for event in original))
