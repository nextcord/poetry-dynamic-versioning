__all__ = []  # type: ignore

import copy
import datetime as dt
import os
import re
import shlex
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from typing import Mapping, MutableMapping, Optional, Sequence, Tuple, Union

import tomlkit

_BYPASS_ENV = "POETRY_DYNAMIC_VERSIONING_BYPASS"
_OVERRIDE_ENV = "POETRY_DYNAMIC_VERSIONING_OVERRIDE"
STAGE_MAPPINGS = {
    "a": "alpha",
    "b": "beta",
    "c": "candidate",
    "rc": "candidate",
}


class _ProjectState:
    def __init__(
        self,
        path: Path,
        original_version: str,
        version: str,
        substitutions: MutableMapping[Path, str] = None,
    ) -> None:
        self.path = path
        self.original_version = original_version
        self.version = version
        self.substitutions = {} if substitutions is None else substitutions


class _State:
    def __init__(self) -> None:
        self.patched_core_poetry_create = False
        self.cli_mode = False
        self.projects = {}  # type: MutableMapping[str, _ProjectState]


_state = _State()


class _SubPattern:
    def __init__(self, value: str, mode: str):
        self.value = value
        self.mode = mode

    @staticmethod
    def from_config(config: Sequence[Union[str, Mapping]]) -> Sequence["_SubPattern"]:
        patterns = []

        for x in config:
            if isinstance(x, str):
                patterns.append(_SubPattern(x, mode="str"))
            else:
                patterns.append(_SubPattern(x["value"], mode=x.get("mode", "str")))

        return patterns


class _FolderConfig:
    def __init__(self, path: Path, files: Sequence[str], patterns: Sequence[_SubPattern]):
        self.path = path
        self.files = files
        self.patterns = patterns

    @staticmethod
    def from_config(config: Mapping, root: Path) -> Sequence["_FolderConfig"]:
        files = config["substitution"]["files"]
        patterns = _SubPattern.from_config(config["substitution"]["patterns"])

        main = _FolderConfig(root, files, patterns)
        extra = [
            _FolderConfig(
                root / x["path"],
                x.get("files", files),
                _SubPattern.from_config(x["patterns"]) if "patterns" in x else patterns,
            )
            for x in config["substitution"]["folders"]
        ]

        return [main, *extra]


def _default_config() -> Mapping:
    return {
        "tool": {
            "poetry-dynamic-versioning": {
                "enable": False,
                "vcs": "any",
                "dirty": False,
                "pattern": None,
                "latest-tag": False,
                "substitution": {
                    "files": ["*.py", "*/__init__.py", "*/__version__.py", "*/_version.py"],
                    "patterns": [
                        r"(^__version__\s*(?::.*?)?=\s*['\"])[^'\"]*(['\"])",
                        {
                            "value": r"(^__version_tuple__\s*(?::.*?)?=\s*\()[^)]*(\))",
                            "mode": "tuple",
                        },
                    ],
                    "folders": [],
                },
                "style": None,
                "metadata": None,
                "format": None,
                "format-jinja": None,
                "format-jinja-imports": [],
                "bump": False,
                "tagged-metadata": False,
                "full-commit": False,
                "tag-branch": None,
                "tag-dir": "tags",
                "strict": False,
                "fix-shallow-repository": False,
            }
        }
    }


def _deep_merge_dicts(base: Mapping, addition: Mapping) -> Mapping:
    result = dict(copy.deepcopy(base))
    for key, value in addition.items():
        if isinstance(value, dict) and key in base and isinstance(base[key], dict):
            result[key] = _deep_merge_dicts(base[key], value)
        else:
            result[key] = value
    return result


def _find_higher_file(*names: str, start: Path = None) -> Optional[Path]:
    # Note: We need to make sure we get a pathlib object. Many tox poetry
    # helpers will pass us a string and not a pathlib object. See issue #40.
    if start is None:
        start = Path.cwd()
    elif not isinstance(start, Path):
        start = Path(start)
    for level in [start, *start.parents]:
        for name in names:
            if (level / name).is_file():
                return level / name
    return None


def _get_pyproject_path(start: Path = None) -> Optional[Path]:
    return _find_higher_file("pyproject.toml", start=start)


def _get_pyproject_path_from_poetry(pyproject) -> Path:
    # poetry-core 1.6.0+:
    recommended = getattr(pyproject, "path", None)
    # poetry-core <1.6.0:
    legacy = getattr(pyproject, "file", None)

    if recommended:
        return recommended
    elif legacy:
        return legacy
    else:
        raise RuntimeError("Unable to determine pyproject.toml path from Poetry instance")


def _get_config(local: Mapping) -> Mapping:
    return _deep_merge_dicts(_default_config(), local)["tool"]["poetry-dynamic-versioning"]


def _get_config_from_path(start: Path = None) -> Mapping:
    pyproject_path = _get_pyproject_path(start)
    if pyproject_path is None:
        return _default_config()["tool"]["poetry-dynamic-versioning"]
    pyproject = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))
    result = _get_config(pyproject)
    return result


def _validate_config(config: Optional[Mapping] = None) -> Sequence[str]:
    if config is None:
        pyproject_path = _get_pyproject_path()
        if pyproject_path is None:
            raise RuntimeError("Unable to find pyproject.toml")
        config = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))

    return _validate_config_section(
        config.get("tool", {}).get("poetry-dynamic-versioning", {}),
        _default_config()["tool"]["poetry-dynamic-versioning"],
        ["tool", "poetry-dynamic-versioning"],
    )


def _validate_config_section(
    config: Mapping, default: Mapping, path: Sequence[str]
) -> Sequence[str]:
    errors = []

    for (key, value) in config.items():
        if key not in default:
            escaped_key = key if "." not in key else '"{}"'.format(key)
            errors.append("Unknown key: " + ".".join([*path, escaped_key]))
        elif isinstance(value, dict) and isinstance(config.get(key), dict):
            errors.extend(_validate_config_section(config[key], default[key], [*path, key]))

    return errors


def _escape_branch(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return re.sub(r"[^a-zA-Z0-9]", "", value)


def _format_timestamp(value: Optional[dt.datetime]) -> Optional[str]:
    if value is None:
        return None
    return value.strftime("%Y%m%d%H%M%S")


def _run_cmd(command: str, codes: Sequence[int] = (0,)) -> Tuple[int, str]:
    result = subprocess.run(
        shlex.split(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    output = result.stdout.decode().strip()
    if codes and result.returncode not in codes:
        raise RuntimeError(
            "The command '{}' returned code {}. Output:\n{}".format(
                command, result.returncode, output
            )
        )
    return (result.returncode, output)


def _get_override_version(name: Optional[str], env: Optional[Mapping] = None) -> Optional[str]:
    env = env if env is not None else os.environ

    if name is not None:
        raw_overrides = env.get(_OVERRIDE_ENV)
        if raw_overrides is not None:
            pairs = raw_overrides.split(",")
            for pair in pairs:
                if "=" not in pair:
                    continue
                k, v = pair.split("=", 1)
                if k.strip() == name:
                    return v.strip()

    bypass = env.get(_BYPASS_ENV)
    if bypass is not None:
        return bypass

    return None


def _get_version_from_dunamai(vcs, pattern, config, *, strict: Optional[bool] = None):
    from dunamai import Version

    return Version.from_vcs(
        vcs,
        pattern,
        config["latest-tag"],
        config["tag-dir"],
        config["tag-branch"],
        config["full-commit"],
        config["strict"] if strict is None else strict,
    )


def _get_version(config: Mapping, name: Optional[str] = None) -> str:
    override = _get_override_version(name)
    if override is not None:
        return override

    import jinja2
    from dunamai import (
        bump_version,
        check_version,
        Concern,
        Pattern,
        serialize_pep440,
        serialize_pvp,
        serialize_semver,
        Style,
        Vcs,
    )

    vcs = Vcs(config["vcs"])
    style = config["style"]
    if style is not None:
        style = Style(style)

    pattern = config["pattern"] if config["pattern"] is not None else Pattern.Default

    if config["fix-shallow-repository"]:
        # We start without strict so we can inspect the concerns.
        version = _get_version_from_dunamai(vcs, pattern, config, strict=False)
        retry = config["strict"]

        if Concern.ShallowRepository in version.concerns and version.vcs == Vcs.Git:
            retry = True
            _run_cmd("git fetch --unshallow")

        if retry:
            version = _get_version_from_dunamai(vcs, pattern, config)
    else:
        version = _get_version_from_dunamai(vcs, pattern, config)

    for concern in version.concerns:
        print("Warning: {}".format(concern.message()), file=sys.stderr)

    if config["format-jinja"]:
        if config["bump"] and version.distance > 0:
            version = version.bump()
        default_context = {
            "base": version.base,
            "version": version,
            "stage": version.stage,
            "revision": version.revision,
            "distance": version.distance,
            "commit": version.commit,
            "dirty": version.dirty,
            "branch": version.branch,
            "branch_escaped": _escape_branch(version.branch),
            "timestamp": _format_timestamp(version.timestamp),
            "env": os.environ,
            "bump_version": bump_version,
            "tagged_metadata": version.tagged_metadata,
            "serialize_pep440": serialize_pep440,
            "serialize_pvp": serialize_pvp,
            "serialize_semver": serialize_semver,
        }
        custom_context = {}  # type: dict
        for entry in config["format-jinja-imports"]:
            if "module" in entry:
                module = import_module(entry["module"])
                if "item" in entry:
                    custom_context[entry["item"]] = getattr(module, entry["item"])
                else:
                    custom_context[entry["module"]] = module
        serialized = jinja2.Template(config["format-jinja"]).render(
            **default_context, **custom_context
        )
        if style is not None:
            check_version(serialized, style)
    else:
        serialized = version.serialize(
            metadata=config["metadata"],
            dirty=config["dirty"],
            format=config["format"],
            style=style,
            bump=config["bump"],
            tagged_metadata=config["tagged-metadata"],
        )

    return serialized


def _substitute_version(name: str, version: str, folders: Sequence[_FolderConfig]) -> None:
    if _state.projects[name].substitutions:
        # Already ran; don't need to repeat.
        return

    files = {}  # type: MutableMapping[Path, _FolderConfig]
    for folder in folders:
        for file_glob in folder.files:
            # call str() since file_glob here could be a non-internable string
            for match in folder.path.glob(str(file_glob)):
                resolved = match.resolve()
                if resolved in files:
                    continue
                files[resolved] = folder

    for file, config in files.items():
        original_content = file.read_text(encoding="utf-8")
        new_content = _substitute_version_in_text(version, original_content, config.patterns)
        if original_content != new_content:
            _state.projects[name].substitutions[file] = original_content
            file.write_bytes(new_content.encode("utf-8"))


def _substitute_version_in_text(version: str, content: str, patterns: Sequence[_SubPattern]) -> str:
    new_content = content

    for pattern in patterns:
        if pattern.mode == "str":
            insert = version
        elif pattern.mode == "tuple":
            parts = []
            split = version.split("+", 1)
            split = [*re.split(r"[-.]", split[0]), *split[1:]]
            for part in split:
                if part == "":
                    continue
                try:
                    parts.append(str(int(part)))
                except ValueError:
                    parts.append('"{}"'.format(part))
            insert = ", ".join(parts)
            if len(parts) == 1:
                insert += ","
        elif pattern.mode == "version_info":
            # This all assumes major.minor.patch[stage{N}][+metadata]
            # Which I am not comfortable in contributing upstream.
            try:
                match = re.match(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<micro>\d+)((?P<stage>a|b|rc)(?P<revision>\d+)?)?(?:\+(?P<metadata>\w+))?$", version)
                if match is None:
                    raise ValueError("Invalid version")
                stage = match.group("stage")
                parts = [
                    match.group("major"),
                    match.group("minor"),
                    match.group("micro"),
                    '"{}"'.format(STAGE_MAPPINGS[stage] if stage else "final"),
                    match.groupdict().get("revision") or "0",
                    '"{}"'.format(match.groupdict().get("metadata", ""))
                ]
                insert = ", ".join(parts)
            except (KeyError, ValueError) as e:
                raise ValueError("Could not parse version into `major.minor.patch[stage{N}][+metadata]`") from e
        else:
            raise ValueError("Invalid substitution mode: {}".format(pattern.mode))

        new_content = re.sub(
            pattern.value, r"\g<1>{}\g<2>".format(insert), new_content, flags=re.MULTILINE
        )

    return new_content


def _apply_version(
    version: str, config: Mapping, pyproject_path: Path, retain: bool = False
) -> None:
    pyproject = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))

    pyproject["tool"]["poetry"]["version"] = version  # type: ignore

    # Disable the plugin in case we're building a source distribution,
    # which won't have access to the VCS info at install time.
    # We revert this later when we deactivate.
    if not retain and not _state.cli_mode:
        pyproject["tool"]["poetry-dynamic-versioning"]["enable"] = False  # type: ignore

    pyproject_path.write_bytes(tomlkit.dumps(pyproject).encode("utf-8"))

    name = pyproject["tool"]["poetry"]["name"]  # type: ignore

    _substitute_version(
        name,  # type: ignore
        version,
        _FolderConfig.from_config(config, pyproject_path.parent),
    )


def _get_and_apply_version(
    name: Optional[str] = None,
    original: Optional[str] = None,
    pyproject: Optional[Mapping] = None,
    pyproject_path: Optional[Path] = None,
    retain: bool = False,
    force: bool = False,
    # fmt: off
    io: bool = True
    # fmt: on
) -> Optional[str]:
    if name is not None and name in _state.projects:
        return name

    if pyproject_path is None:
        pyproject_path = _get_pyproject_path()
        if pyproject_path is None:
            raise RuntimeError("Unable to find pyproject.toml")

    if pyproject is None:
        pyproject = tomlkit.parse(pyproject_path.read_text(encoding="utf-8"))

    if name is None or original is None:
        name = pyproject["tool"]["poetry"]["name"]
        original = pyproject["tool"]["poetry"]["version"]
        if name in _state.projects:
            return name

    config = _get_config(pyproject)
    if not config["enable"] and not force:
        return name if name in _state.projects else None

    initial_dir = Path.cwd()
    target_dir = pyproject_path.parent
    os.chdir(str(target_dir))
    try:
        version = _get_version(config, name)
    finally:
        os.chdir(str(initial_dir))

    # Condition will always be true, but it makes Mypy happy.
    if name is not None and original is not None:
        _state.projects[name] = _ProjectState(pyproject_path, original, version)
        if io:
            _apply_version(version, config, pyproject_path, retain)

    return name


def _revert_version(retain: bool = False) -> None:
    for project, state in _state.projects.items():
        pyproject = tomlkit.parse(state.path.read_text(encoding="utf-8"))
        pyproject["tool"]["poetry"]["version"] = state.original_version  # type: ignore

        if not retain and not _state.cli_mode:
            pyproject["tool"]["poetry-dynamic-versioning"]["enable"] = True  # type: ignore

        state.path.write_bytes(tomlkit.dumps(pyproject).encode("utf-8"))

        if state.substitutions:
            for file, content in state.substitutions.items():
                file.write_bytes(content.encode("utf-8"))

    _state.projects.clear()
