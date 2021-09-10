import logging
import uuid
from pathlib import Path

from flask import Flask, jsonify, request, url_for, redirect
from flask_inputs import Inputs
from flask_inputs.validators import JsonSchema

from fuzzer import FuzzingThread

schema = {
    "type": 'object',
    "properties": {
        "problem": {"type": "string", "minLength": 3},
        "lang": {"enum": ["java", "cpp", "python"]},
        "sources": {
            "type": "object",
            "minProperties": 1,
            "patternProperties": {
                "^.*$": {"type": "string", "minLength": 3}
            }
        },
        "time_limit": {"type": "integer", "minimum": 0}
    },
    "required": ["problem", "lang", "sources", "time_limit"]
}


class JsonInputs(Inputs):
    json = [JsonSchema(schema=schema)]


app = Flask(__name__)

state = dict()


@app.route('/')
def home():
    return redirect(url_for('static', filename='client.html'))


@app.route('/problems')
def show_problems():
    return jsonify(success=True, problems=[problem.name for problem in problems])


@app.route('/status')
def show_status():
    status = {k: v.getstate() for k, v in state.items()}
    return jsonify(success=True, status=status)


@app.route('/submission', methods=['POST'])
def start_fuzzing():
    inputs = JsonInputs(request)
    if not inputs.validate():
        app.logger.debug("Invalid JSON request: %s", request)
        return jsonify(success=False, errors=inputs.errors)

    data = request.get_json()

    fuzzing_id = str(uuid.uuid4())
    state[fuzzing_id] = FuzzingThread(fuzzing_id, app.logger, data, 500, problem_repository)
    state[fuzzing_id].start()

    return jsonify(success=True, id=fuzzing_id)


@app.route('/submission/<fuzzing_id>', methods=['GET'])
def show_single_status(fuzzing_id):
    if fuzzing_id not in state:
        return jsonify(success=False, errors=["Id not found"])
    return jsonify(success=True, state=state[fuzzing_id].getstate())


@app.route('/submission/<fuzzing_id>', methods=['DELETE'])
def stop_fuzzing(fuzzing_id):
    fuzzer = state.pop(fuzzing_id, None)

    if fuzzer is None:
        return jsonify(success=False, state=None)

    fuzzer_state = fuzzer.getstate()
    fuzzer.destroy()

    return jsonify(success=True, state=fuzzer_state)


if __name__ == '__main__':
    # Root logger
    LOGGING_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

    # Problem tools use root logger -.- use this line to silence them
    # logging.basicConfig(level=logging.CRITICAL, format=LOGGING_FORMAT)
    logging.basicConfig(level=logging.DEBUG, format=LOGGING_FORMAT)

    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("submission").setLevel(logging.DEBUG)

    problem_repository = Path("problems")
    problems = [path.parent for path in problem_repository.glob("*/problem.yaml")]

    app.run()
