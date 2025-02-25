import collections
import inspect
import io
import json
import os
import pathlib
import sys
import tempfile
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple, TypeVar, Union
from unittest import mock

import pytest
import requests
import requests_mock
from responses import matchers

from determined.cli import cli, ntsc, render
from determined.common import constants, context
from determined.common.api import bindings
from tests import filetree
from tests.cli import util

MINIMAL_CONFIG = '{"description": "test"}'
MASTER_HOST = "http://localhost:8080"


def test_parse_config() -> None:
    assert ntsc.parse_config(None, [], [], []) == {}

    config = ["resources.slots=4"]
    assert ntsc.parse_config(None, ["python", "train.py"], config, []) == {
        "resources": {"slots": 4},
        "entrypoint": ["python", "train.py"],
    }

    config = [
        "resources.slots=4",
    ]
    assert ntsc.parse_config(None, ["python", "train.py"], config, []) == {
        "resources": {"slots": 4},
        "entrypoint": ["python", "train.py"],
    }

    config = ["""bind_mounts=host_path: /bin\ncontainer_path: /foo-bar"""]
    assert ntsc.parse_config(None, [], config, ["/bin:/foo-bar2"]) == {
        "bind_mounts": [
            {"host_path": "/bin", "container_path": "/foo-bar"},
            {"host_path": "/bin", "container_path": "/foo-bar2"},
        ]
    }


# mock_experiment was derived from a real v1Experiment.to_json(), with none of the optional fields.
mock_experiment = {
    "archived": False,
    "config": {},
    "id": 1,
    "jobId": "c659255d-7f8c-408e-aad6-08bee6915b08",
    "name": "mock-experiment",
    "numTrials": 1,
    "originalConfig": "",
    "projectId": 1,
    "projectOwnerId": 1,
    "searcherType": "single",
    "startTime": "2022-12-07T21:27:21.985656Z",
    "state": "STATE_ACTIVE",
    "username": "determined",
}


def test_create_with_model_def(requests_mock: requests_mock.Mocker, tmp_path: pathlib.Path) -> None:
    requests_mock.get("/info", status_code=200, json={"version": "1.0"})

    requests_mock.get(
        "/api/v1/me", status_code=200, json={"username": constants.DEFAULT_DETERMINED_USER}
    )

    fake_user = {"username": "fakeuser", "admin": True, "active": True}
    requests_mock.post(
        "/api/v1/auth/login", status_code=200, json={"token": "fake-token", "user": fake_user}
    )

    requests_mock.post(
        "/api/v1/experiments",
        status_code=requests.codes.ok,
        headers={"Location": "/experiments/1"},
        json={
            "experiment": mock_experiment,
            "config": mock_experiment["config"],
            "jobSummary": None,
        },
    )

    tempfile.mkstemp(dir=str(tmp_path))

    with filetree.FileTree(tmp_path, {"config.yaml": MINIMAL_CONFIG}) as tree:
        cli.main(
            ["experiment", "create", "--paused", str(tree.joinpath("config.yaml")), str(tmp_path)]
        )


def test_uuid_prefix(requests_mock: requests_mock.Mocker) -> None:
    # Create two UUIDs that are different at a known index.
    fake_uuid1 = str(uuid.uuid4())
    replace_ind = 4
    fake_uuid2 = (
        fake_uuid1[:replace_ind]
        + ("1" if fake_uuid1[replace_ind] == "0" else "0")
        + fake_uuid1[replace_ind + 1 :]
    )

    requests_mock.get("/info", status_code=200, json={"version": "1.0"})
    requests_mock.get(
        "/api/v1/me", status_code=200, json={"username": constants.DEFAULT_DETERMINED_USER}
    )

    fake_user = {"username": "fakeuser", "admin": True, "active": True}
    requests_mock.post(
        "/api/v1/auth/login", status_code=200, json={"token": "fake-token", "user": fake_user}
    )

    requests_mock.get(
        "/api/v1/shells",
        status_code=requests.codes.ok,
        json={"shells": [{"id": fake_uuid1}, {"id": fake_uuid2}]},
    )

    requests_mock.get(
        f"/api/v1/shells/{fake_uuid1}",
        status_code=requests.codes.ok,
        json={"config": None},
    )

    # Succeed with a full UUID.
    cli.main(["shell", "config", fake_uuid1])
    # Succeed with a partial unique prefix.
    cli.main(["shell", "config", fake_uuid1[: replace_ind + 1]])
    # Fail with an existing but nonunique prefix.
    with pytest.raises(SystemExit):
        cli.main(["shell", "config", fake_uuid1[:replace_ind]])
    # Fail with a nonexistent prefix.
    with pytest.raises(SystemExit):
        cli.main(["shell", "config", "x"])


def test_create_reject_large_model_def(
    requests_mock: requests_mock.Mocker, tmp_path: pathlib.Path
) -> None:
    requests_mock.get("/info", status_code=200, json={"version": "1.0"})

    requests_mock.get(
        "/api/v1/me", status_code=200, json={"username": constants.DEFAULT_DETERMINED_USER}
    )

    requests_mock.post(
        "/experiments", status_code=requests.codes.created, headers={"Location": "/experiments/1"}
    )

    with tempfile.NamedTemporaryFile() as model_def_file:
        model_def_file.write(os.urandom(constants.MAX_CONTEXT_SIZE + 1))
        with filetree.FileTree(tmp_path, {"config.yaml": MINIMAL_CONFIG}) as tree, pytest.raises(
            SystemExit
        ):
            cli.main(
                ["experiment", "create", str(tree.joinpath("config.yaml")), model_def_file.name]
            )


def test_read_context(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(tmp_path, {"A.py": "", "B.py": "", "C.py": ""}) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "B.py", "C.py"}


def test_read_context_with_detignore(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(tmp_path, {"A.py": "", "B.py": "", "C.py": ""}) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "B.py", "C.py"}

    with filetree.FileTree(
        tmp_path, {"A.py": "", "B.py": "", "C.py": "", ".detignore": "\nA.py\n"}
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"B.py", "C.py"}

    with filetree.FileTree(
        tmp_path, {"A.py": "", "B.py": "", "C.py": "", ".detignore": "\n*.py\n"}
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert model_def == []


def test_read_context_with_detignore_subdirs(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "B.py": "",
            pathlib.Path("subdir").joinpath("A.py"): "",
            pathlib.Path("subdir").joinpath("B.py"): "",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {
            "A.py",
            "B.py",
            "subdir",
            "subdir/A.py",
            "subdir/B.py",
        }

    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "B.py": "",
            ".detignore": "\nA.py\n",
            pathlib.Path("subdir").joinpath("A.py"): "",
            pathlib.Path("subdir").joinpath("B.py"): "",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"B.py", "subdir", "subdir/B.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "B.py": "",
            pathlib.Path("subdir").joinpath("A.py"): "",
            pathlib.Path("subdir").joinpath("B.py"): "",
            ".detignore": "\nsubdir/A.py\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "B.py", "subdir", "subdir/B.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "B.py": "",
            pathlib.Path("subdir").joinpath("A.py"): "",
            pathlib.Path("subdir").joinpath("B.py"): "",
            ".detignore": "\n*.py\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert len(model_def) == 1

    with filetree.FileTree(
        tmp_path,
        {"A.py": "", "B.py": "", "subdir/A.py": "", "subdir/B.py": "", ".detignore": "\nsubdir\n"},
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "B.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "B.py": "",
            "subdir/A.py": "",
            "subdir/B.py": "",
            ".detignore": "\nsubdir/\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "B.py"}


def test_read_context_with_detignore_wildcard(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(
        tmp_path,
        {
            "dir/file.py": "",
            "dir/subdir/A.py": "",
            "dir/subdir/B.py": "",
            "dir/subdir/subdir/subdir/C.py": "",
            ".detignore": "\ndir/sub*/\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"dir", "dir/file.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "dir/file.py": "",
            "dir/subdir/A.py": "",
            "dir/subdir/B.py": "",
            "dir/subdir/subdir/subdir/C.py": "",
            ".detignore": "\ndir/sub*\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"dir", "dir/file.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "dir/file.py": "",
            "dir/subdir/A.py": "",
            "dir/subdir/B.py": "",
            "dir/subdir/subdir/subdir/C.py": "",
            ".detignore": "\ndir/*/\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"dir", "dir/file.py"}

    with filetree.FileTree(
        tmp_path,
        {
            "dir/file.py": "",
            "dir/subdir/A.py": "",
            "dir/subdir/B.py": "",
            "dir/subdir/subdir/subdir/C.py": "",
            ".detignore": "\ndir/*\n",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"dir"}


def test_read_context_ignore_pycaches(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(
        tmp_path,
        {
            "__pycache__/A.cpython-37.pyc": "",
            "A.py": "",
            "subdir/A.py": "",
            "subdir/__pycache__/A.cpython-37.pyc": "",
        },
    ) as tree:
        model_def = context.read_legacy_context(tree)
        assert {f["path"] for f in model_def} == {"A.py", "subdir", "subdir/A.py"}


def test_includes(tmp_path: pathlib.Path) -> None:
    with filetree.FileTree(
        tmp_path,
        {
            "A.py": "",
            "dir/B.py": "",
            "context/C.py": "",
        },
    ) as tree:
        # Directory name is stripped for contexts, preserved for includes.
        model_def = context.read_legacy_context(
            context_root=tree / "context",
            includes=[tree / "A.py", tree / "dir"],
        )
        assert {f["path"] for f in model_def} == {"A.py", "dir", "dir/B.py", "C.py"}

        # Includes without a context is supported.
        model_def = context.read_legacy_context(
            context_root=None,
            includes=[tree / "A.py", tree / "dir"],
        )
        assert {f["path"] for f in model_def} == {"A.py", "dir", "dir/B.py"}

        # Disallow context-include conflicts.
        with pytest.raises(ValueError):
            context.read_legacy_context(context_root=tree, includes=[tree / "A.py"])
        with pytest.raises(ValueError):
            context.read_legacy_context(context_root=tree, includes=[tree / "dir"])

        # Disallow include-include conflicts.
        with pytest.raises(ValueError):
            context.read_legacy_context(context_root=None, includes=[tree / "A.py", tree / "A.py"])
        with pytest.raises(ValueError):
            context.read_legacy_context(context_root=None, includes=[tree / "dir", tree / "dir"])


def test_cli_args_exist() -> None:
    valid_cmds = [
        "auth",
        "agent",
        "a",
        "command",
        "cmd",
        "checkpoint",
        "c",
        "deploy",
        "d",
        "experiment",
        "e",
        "master",
        "m",
        "model",
        "m",
        "notebook",
        "oauth",
        "resources",
        "res",
        "shell",
        "slot",
        "s",
        "task",
        "template",
        "tpl",
        "tensorboard",
        "trial",
        "t",
        "user",
        "u",
    ]
    for cmd in valid_cmds:
        cli.main([cmd, "help"])

    cli.main([])
    for cmd in ["version", "help"]:
        cli.main([cmd])

    with pytest.raises(SystemExit) as e:
        cli.main(["preview-search", "-h"])
    assert e.value.code == 0


Case = collections.namedtuple("Case", ["input", "output", "colors"])
color_test_cases: List[Case] = [
    Case(1, "1", ["PRIMITIVES"]),
    Case(1.0, "1.0", ["PRIMITIVES"]),
    Case(True, "true", ["PRIMITIVES"]),
    Case(False, "false", ["PRIMITIVES"]),
    Case(None, "null", ["PRIMITIVES"]),
    Case([], "[]", ["SEPARATORS"]),
    Case({}, "{}", ["SEPARATORS"]),
    Case((), "[]", ["SEPARATORS"]),
    Case("foo", '"foo"', ["STRING"]),
    Case([1], "[\n  1\n]", ["SEPARATORS", "PRIMITIVES", "SEPARATORS"]),
    Case(
        {"foo": 1},
        '{\n  "foo": 1\n}',
        ["SEPARATORS", "KEY", "SEPARATORS", "PRIMITIVES", "SEPARATORS"],
    ),
    Case((1,), "[\n  1\n]", ["SEPARATORS", "PRIMITIVES", "SEPARATORS"]),
]


@mock.patch("termcolor.colored")
@pytest.mark.parametrize("case", color_test_cases)
def test_colored_color_values(mocked_colored: mock.Mock, case: Case) -> None:
    stream = mock.Mock()
    render.render_colorized_json(case.input, stream)
    calls = mocked_colored.mock_calls
    assert len(calls) == len(case.colors)
    for call, color_type in zip(calls, case.colors):
        assert call[1][1] == render.COLORS[color_type], call
    mocked_colored.reset_mock()


@pytest.mark.parametrize("case", color_test_cases)
def test_colored_str_output(case: Case) -> None:
    stream = io.StringIO()
    render.render_colorized_json(case.input, stream, indent="  ")
    assert stream.getvalue() == case.output + "\n"


@pytest.mark.skipif(sys.version_info < (3, 8), reason="requires Python3.8 or higher")
def test_dev_unwrap_optional() -> None:
    from determined.cli import dev

    annots = [
        Tuple[int, str],
        int,
        str,
        float,
        bool,
    ]
    for annot in annots:
        assert dev.unwrap_optional(annot) == annot
        assert dev.unwrap_optional(Optional[annot]) == annot
        assert dev.unwrap_optional(Union[annot, None]) == annot

    cases = [
        ("bool", bool),
        (Optional[bool], bool),
        ("Optional[bool]", bool),
        ("Optional[str]", str),
        ("List[str]", List[str]),
        ("typing.Union[str, NoneType]", str),
    ]
    for annot, expected in cases:
        assert dev.unwrap_optional(annot) == expected, annot


@pytest.mark.skipif(sys.version_info < (3, 8), reason="requires Python3.8 or higher")
def test_dev_bindings_parameter_inspect() -> None:
    from determined.cli import dev

    ComplexType = TypeVar("ComplexType")

    unsupported = [ComplexType, Tuple[ComplexType, ...], Tuple[int, str]]
    unsupported.extend([str(t) for t in unsupported])

    supported = [
        Optional[str],
        Sequence[str],
        Optional[Sequence[str]],
    ]
    supported.extend([str(t) for t in supported])

    annot_expected: List[Tuple[Any, bool]] = []
    annot_expected.extend([(a, False) for a in unsupported])
    annot_expected.extend([(a, True) for a in supported])

    for annot, expected in annot_expected:
        assert dev.is_supported_annotation(annot) is expected, f"{annot} expected: {expected}"

        param1: inspect.Parameter = inspect.Parameter(
            "sth_with_default",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=annot,
            default=None,
        )
        assert dev.can_be_called_via_cli([param1]) is True, param1

        param1 = inspect.Parameter(
            "no_default",
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=annot,
            default=inspect.Parameter.empty,
        )
        assert dev.can_be_called_via_cli([param1]) is expected, param1


args_sets = [
    (
        ["[1, 2]", "3"],
        {
            "ids": [1, 2],
            "periodSeconds": 3,
        },
    ),
    (
        ["ids=[1, 2]", "3"],
        {
            "ids": [1, 2],
            "periodSeconds": 3,
        },
    ),
    (
        ["ids=[1, 2]", "periodSeconds=3"],
        {
            "ids": [1, 2],
            "periodSeconds": 3,
        },
    ),
    (
        ["periodSeconds=3"],
        {
            "periodSeconds": 3,
        },
    ),
]


@pytest.mark.skipif(sys.version_info < (3, 8), reason="requires Python3.8 or higher")
@pytest.mark.parametrize("case", args_sets)
def test_dev_bindings_call_arg_unmarshal(case: Tuple[List[str], Dict[str, Any]]) -> None:
    from determined.cli import dev

    args, expected = case
    for a in args:
        assert isinstance(a, str), a

    _, params = dev.bindings_sig(bindings.get_ExpMetricNames)
    kwargs = dev.parse_args_to_kwargs(args, params)
    assert kwargs == expected, kwargs


def test_preview_search(tmp_path: pathlib.Path) -> None:
    # Random
    max_trials = 10
    searcher_config = {
        "hyperparameters": {
            "x": 12,
        },
        "name": "test preview search (random)",
        "searcher": {
            "name": "random",
            "metric": "loss",
            "max_trials": max_trials,
        },
    }
    conf_path = tmp_path / "config.yaml"
    with conf_path.open("w") as tmp_file:
        tmp_file.write(json.dumps(searcher_config))

    mock_resp = bindings.v1PreviewHPSearchResponse(
        summary=bindings.v1SearchSummary(
            config=searcher_config,
            trials=[
                bindings.v1TrialSummary(
                    count=max_trials,
                    unit=bindings.v1SearchUnit(maxLength=True),
                )
            ],
        )
    )
    with util.standard_cli_rsps() as rsps:
        rsps.post(
            f"{MASTER_HOST}/api/v1/preview-hp-search",
            status=200,
            match=[
                matchers.json_params_matcher(
                    params={
                        "config": searcher_config,
                    }
                )
            ],
            json=mock_resp.to_json(),
        )
        expected_output = f"""Using search configuration:
{render.format_object_as_yaml(searcher_config)}
   Trials | Training Time
----------+---------------------
       10 | train to completion
"""
        util.check_cli_output(["preview-search", str(conf_path)], expected_output)

    # ASHA
    searcher_config = {
        "hyperparameters": {
            "x": 12,
        },
        "name": "test preview search (asha)",
        "searcher": {
            "bracket_rungs": [],
            "divisor": 5,
            "max_concurrent_trials": 5,
            "max_rungs": 5,
            "max_time": 1000,
            "max_trials": 10,
            "metric": "loss",
            "mode": "standard",
            "name": "adaptive_asha",
            "time_metric": "batch",
        },
    }
    conf_path = tmp_path / "config.yaml"
    with conf_path.open("w") as tmp_file:
        tmp_file.write(json.dumps(searcher_config))

    mock_resp = bindings.v1PreviewHPSearchResponse(
        summary=bindings.v1SearchSummary(
            config=searcher_config,
            trials=[
                bindings.v1TrialSummary(
                    count=7,
                    unit=bindings.v1SearchUnit(name="batch", value=200, maxLength=False),
                ),
                bindings.v1TrialSummary(
                    count=3,
                    unit=bindings.v1SearchUnit(name="batch", value=1000, maxLength=False),
                ),
            ],
        )
    )
    with util.standard_cli_rsps() as rsps:
        rsps.post(
            f"{MASTER_HOST}/api/v1/preview-hp-search",
            status=200,
            match=[
                matchers.json_params_matcher(
                    params={
                        "config": searcher_config,
                    }
                )
            ],
            json=mock_resp.to_json(),
        )
        expected_output = f"""Using search configuration:
{render.format_object_as_yaml(searcher_config)}
   Trials | Training Time
----------+----------------------
        7 | train for 200 batch
        3 | train for 1000 batch
"""
        util.check_cli_output(["preview-search", str(conf_path)], expected_output)
