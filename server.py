import logging
import pathlib
import sys
import threading
import uuid
import argparse
from io import StringIO
from typing import List, Dict

from flask import Flask, jsonify, request, url_for, redirect
from flask_inputs import Inputs
from flask_inputs.validators import JsonSchema

from fuzzer import FuzzingRequest, Fuzzer

from pyjudge.repository.kattis import RepositoryProblem, Repository

schema = {
    "type": 'object',
    "properties": {
        "problem": {"type": "string", "minLength": 3},
        "language": {"enum": ["cpp", "haskell", "java", "javascript", "julia", "pascal", "python", "rust"]},
        "sources": {
            "type": "object",
            "minProperties": 1,
            "patternProperties": {
                "^.*$": {"type": "string", "minLength": 3}
            }
        },
        "case_name": {"type": "string"},
        "runs": {"type": "integer", "minimum": 0}
    },
    "required": ["problem", "language", "sources", "case_name"]
}


class JsonInputs(Inputs):
    json = [JsonSchema(schema=schema)]


# Root logger
LOGGING_FORMAT = '%(asctime)s - %(name)s - %(levelname)s %(message)s'

logging.basicConfig(level=logging.DEBUG, format=LOGGING_FORMAT)

logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)
logging.getLogger("fuzzer").setLevel(logging.DEBUG)

app = Flask(__name__)


class FuzzingThread(threading.Thread):
    FORMATTER = logging.Formatter("%(message)s")

    def __init__(self, fuzzer_id, submission, repository):
        threading.Thread.__init__(self)

        self.fuzzer_id = fuzzer_id
        self.submission = submission
        self.submission["valid"] = True
        self.state = {'id': self.fuzzer_id, 'finished': False}
        self.repository = repository
        self.log_stream = StringIO()

    def run(self):
        submission_logger = logging.getLogger(f"submission.{self.fuzzer_id}")
        submission_log_handler = logging.StreamHandler(self.log_stream)
        submission_log_handler.setFormatter(self.FORMATTER)
        for handler in list(submission_logger.handlers):
            submission_logger.removeHandler(handler)
        submission_logger.addHandler(submission_log_handler)
        submission_logger.setLevel(level=logging.DEBUG)

        try:
            problem = self.repository.problems[self.submission["problem"]]
            seed_file = problem.directory / "data" / "secret" / f"{self.submission['case_name']}.seed"
            request = FuzzingRequest(
                sources=self.submission["sources"],
                language=self.submission["language"],
                problem=problem,
                seed_file=seed_file,
                logger=submission_logger,
                run_count=self.submission.get('runs', 10)
            )
            result = fuzzer.run(request)
            if result is not None:
                cases = {}
                for index, run_result in enumerate(result.run_results):
                    cases[f"{index + 1}_{run_result.verdict}"] = {
                        "case.in": run_result.input,
                        "case.ans": run_result.answer
                    }
                self.state['cases'] = cases
                self.state['log'] = self.log_stream.getvalue()
            logging.info("Finished fuzzing run %s", self.fuzzer_id)
        except Exception as e:
            logging.warning("Unexpected error", exc_info=e)
            submission_logger.error("Unexpected error: %s", e)
        finally:
            submission_log_handler.flush()
            self.log_stream.close()
            self.state['finished'] = True

    def get_state(self):
        state = self.state.copy()
        if not state['finished']:
            state['log'] = self.log_stream.getvalue()
        return state


class FuzzingManager(object):
    def __init__(self, repository: "Repository"):
        self.repository = repository
        self.state: Dict[str, FuzzingThread] = {}
        pass

    def run(self, submission):
        fuzzing_id = str(uuid.uuid4())
        thread = FuzzingThread(fuzzing_id, submission, self.repository)
        self.state[fuzzing_id] = thread
        thread.start()
        return fuzzing_id


@app.route('/')
def home():
    return redirect(url_for('static', filename='client.html'))


@app.route('/problems')
def show_problems():
    return jsonify(success=True, problems=[p.repository_key for p in problems])


@app.route('/problem/<problem_name>/seeds', methods=['GET'])
def get_problem_seeds(problem_name):
    secret_path = repository.problems.load_problem(problem_name).directory / "data" / "secret"
    if not secret_path.is_dir():
        return jsonify(success=False, errors=["No such problem"])
    seeds = [seed.name[:-5] for seed in secret_path.glob("*.seed")]
    return jsonify(success=True, seeds=seeds)


@app.route('/status')
def show_status():
    status = {k: v.get_state() for k, v in manager.state.items()}
    return jsonify(success=True, status=status)


@app.route('/submission', methods=['POST'])
def start_fuzzing():
    inputs = JsonInputs(request)
    if not inputs.validate():
        app.logger.debug("Invalid JSON request: %s", request)
        return jsonify(success=False, errors=inputs.errors)

    fuzzing_id = manager.run(submission=request.get_json())
    return jsonify(success=True, id=fuzzing_id)


@app.route('/submission/<fuzzing_id>', methods=['GET'])
def show_single_status(fuzzing_id):
    if fuzzing_id not in manager.state:
        return jsonify(success=False, errors=["Id not found"])
    return jsonify(success=True, state=manager.state[fuzzing_id].get_state())


@app.route('/submission/<fuzzing_id>', methods=['DELETE'])
def stop_fuzzing(fuzzing_id):
    fuzzer = manager.state.pop(fuzzing_id, None)

    if fuzzer is None:
        return jsonify(success=False, state=None)

    fuzzer_state = fuzzer.get_state()
    return jsonify(success=True, state=fuzzer_state)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-r", "--repository", help="Path to repository", type=pathlib.Path, required=True)
    args = parser.parse_args()
    repository_path: pathlib.Path = args.repository

    if not repository_path.is_dir():
        sys.exit(f"Path {repository_path} is not a path")

    repository = Repository(repository_path)
    problems: List[RepositoryProblem] = []
    for problem in repository.problems:
        secret_dir = problem.directory / "data" / "secret"
        if any(True for secret in secret_dir.glob("*.seed")):
            problems.append(problem)

    if not problems:
        sys.exit("Found no valid problems!")

    logging.getLogger().info("Found %d problems with seeds", len(problems))

    fuzzer = Fuzzer()
    manager = FuzzingManager(repository)
    app.run()
