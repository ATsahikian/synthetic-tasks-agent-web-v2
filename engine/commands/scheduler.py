"""Accept commands and advance their durable action queues."""

import copy
import uuid

from .registry import handler_for
from .state import resolve_parameters
from ..actions import fingerprint, validate_name
from ..errors import EngineError


def normalize_request(body):
    from ..core import validate_body

    if isinstance(body, dict) and 'action' in body:
        validate_body(body, ('request_id', 'action'), ('parameters',))
        validate_name(body['action'])
        request = {'request_id': body['request_id'], 'action': body['action'], 'parameters': copy.deepcopy(body.get('parameters', {}))}
        return request, 'command', 'one-action', {'action': request['action'], 'action_parameters': request['parameters']}
    validate_body(body, ('request_id', 'command'), ('parameters',))
    validate_name(body['command'])
    request = {'request_id': body['request_id'], 'command': body['command'], 'parameters': copy.deepcopy(body.get('parameters', {}))}
    return request, 'run_command', request['command'], request['parameters']


class CommandScheduler:
    def __init__(self, engine):
        self.engine = engine

    def accept(self, task_set, task, body):
        request, operation, name, parameters = normalize_request(body)
        if not isinstance(parameters, dict):
            raise EngineError('parameters must be an object.')
        engine = self.engine
        with engine._lock:
            engine._check_available()
            if engine._stopping:
                raise EngineError('Engine is stopping.', status=503, code='engine_unavailable')
            key = engine._key(task_set, task)
            previous = engine._duplicate(key, request['request_id'], fingerprint(operation, request))
            if previous is not None:
                return previous
            engine._ensure_discovered(key)
            state = engine._states[key]
            if state['active_command_id'] or any(a['status'] != 'completed' for a in state['actions'].values()):
                raise EngineError('A command or historical action already owns this task.', status=409, code='command_already_active')
            definition = engine.commands.get(name)
            if definition is None:
                raise EngineError('Unknown predefined command.', status=404, code='command_not_found')
            steps = handler_for(definition['handler']).prepare(definition, engine.actions, parameters)
            command_id = str(uuid.uuid4())
            receipt = engine._commit_command(
                key, request, operation, 'command_started', command_id, None,
                {'command': name, 'parameters': copy.deepcopy(parameters), 'steps': steps},
            )
            self.advance(key)
            return receipt

    def _cancel_pending(self, key, command, error, failed_action_id):
        for action_id in command['action_ids']:
            if self.engine._states[key]['actions'][action_id]['status'] == 'queued':
                self.engine._commit_command(
                    key, {'request_id': f'engine:cancelled:{action_id}', 'error': error, 'failed_action_id': failed_action_id}, 'cancel_pending',
                    'cancelled', command['command_id'], action_id,
                    {'error': error, 'failed_action_id': failed_action_id},
                )

    def advance(self, key):
        engine = self.engine
        while engine._states[key]['active_command_id']:
            engine._check_available()
            state = engine._states[key]
            command = state['commands'][state['active_command_id']]
            actions = [state['actions'][identity] for identity in command['action_ids']]
            failure = next((a for a in actions if a['status'] == 'completed' and a['outcome'] != 'succeeded'), None)
            cancelled = command['cancel_requested'] or (failure is not None and failure['outcome'] == 'cancelled')
            unavailable_task = key not in engine._discovered
            error = (failure['error'] or f"Action {failure['name']} ended with outcome {failure['outcome']}.") if failure else None
            failed_action_id = failure['action_id'] if failure else None
            if command['cancel_requested']:
                error = 'Command cancellation requested.'
            elif unavailable_task and failure is None:
                error = 'Task is no longer present in the input catalog.'
            if cancelled or failure or unavailable_task:
                self._cancel_pending(key, command, error, failed_action_id)
                running = [a for a in actions if a['status'] == 'started']
                if running:
                    if command['cancel_requested']:
                        engine._runner.cancel(key, running[0]['action_id'])
                    return
                self._finish(key, command, 'cancelled' if cancelled else 'failed', error, failed_action_id)
                return
            pending = [a for a in actions if a['status'] != 'completed']
            if not pending:
                self._finish(key, command, 'succeeded', None, None)
                return
            action = pending[0]
            if action['status'] == 'started':
                return
            if engine._stopping:
                return
            step = next(step for step in command['steps'] if step['action_id'] == action['action_id'])
            parameters = resolve_parameters(state, command, step)
            if action['mode'] == 'sync':
                captured = copy.deepcopy(action)
                captured['parameters'] = parameters
                result = engine._execute_sync(key, captured)
                self.complete(key, {'request_id': f"engine:completion:{action['action_id']}", 'action_id': action['action_id'], 'type': 'completed', **result})
            else:
                engine._commit_command(
                    key, {'request_id': f"engine:start:{action['action_id']}", 'action_id': action['action_id'], 'type': 'started'},
                    'start', 'started', command['command_id'], action['action_id'], {'parameters': parameters},
                )
                engine._initiate(key, action['action_id'])

    def complete(self, key, request):
        engine = self.engine
        engine._check_available()
        state = engine._states[key]
        action = state['actions'].get(request['action_id'])
        if action is None:
            raise EngineError('Unknown action ID.', status=404, code='action_not_found')
        if action['status'] == 'completed':
            raise EngineError('Action is already completed.', status=409, code='action_already_completed')
        command = state['commands'][action['command_id']]
        step = next(step for step in command['steps'] if step['action_id'] == action['action_id'])
        return engine._commit_command(
            key, request, 'report', 'completed', action['command_id'], action['action_id'],
            {'parameters': resolve_parameters(state, command, step), **{name: request[name] for name in ('outcome', 'result', 'error')}},
        )

    def _finish(self, key, command, outcome, error, failed_action_id):
        actions = self.engine._states[key]['actions']
        result = {'actions': [
            {'action_id': identity, 'action': actions[identity]['name'], **{name: copy.deepcopy(actions[identity][name]) for name in ('outcome', 'result', 'error')}}
            for identity in command['action_ids']
        ]}
        self.engine._commit_command(
            key, {'request_id': f"engine:command-completion:{command['command_id']}", 'outcome': outcome, 'result': result, 'error': error, 'failed_action_id': failed_action_id}, 'finish_command',
            'command_completed', command['command_id'], None,
            {'outcome': outcome, 'result': result, 'error': error, 'failed_action_id': failed_action_id},
        )

    def cancel(self, task_set, task, body):
        from ..core import validate_body
        from .legacy import cancel_action

        if isinstance(body, dict) and 'action_id' in body:
            validate_body(body, ('request_id', 'action_id'))
        else:
            validate_body(body, ('request_id', 'command_id'))
        engine = self.engine
        with engine._lock:
            engine._check_available()
            key = engine._key(task_set, task)
            previous = engine._duplicate(key, body['request_id'], fingerprint('cancel', body))
            if previous is not None:
                return previous
            if 'action_id' in body:
                action = engine._states[key]['actions'].get(body['action_id']) if isinstance(body['action_id'], str) else None
                if action is None:
                    raise EngineError('Unknown action ID.', status=404, code='action_not_found')
                if not action.get('command_id'):
                    return cancel_action(engine, key, body)
                command_id = action['command_id']
            else:
                command_id = body['command_id']
            if not isinstance(command_id, str):
                raise EngineError('command_id must be a UUID string.')
            command = engine._states[key]['commands'].get(command_id)
            if command is None:
                raise EngineError('Unknown command ID.', status=404, code='command_not_found')
            receipt = engine._commit_command(key, copy.deepcopy(body), 'cancel', 'command_cancel_requested', command_id, None, {})
            self.advance(key)
            return receipt
