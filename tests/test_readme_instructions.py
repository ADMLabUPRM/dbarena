import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
COMPOSE_PATHS = (
    REPO_ROOT / "docker-compose.yml",
    REPO_ROOT / "docker-compose.access.yml",
    REPO_ROOT / "docker-compose.embed-gpu.yml",
)
MAKEFILE_PATH = REPO_ROOT / "Makefile"
ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

PORT_VARS = ("FRONTEND_PORT", "BACKEND_PORT", "MCP_PORT", "ADMINDB_PORT", "TASKDB_PORT", "RAGDB_PORT")
EMBEDDING_VARS = ("EMBEDDING_BASE_URL", "EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_DIM")
DOCUMENTED_SERVICES = ("admindb", "taskdb", "ragdb", "olapdb", "dsdb", "backend", "frontend", "mcp", "judge",
                       "embed-server")
NAMED_VOLUMES = ("admindb_data", "taskdb_data", "ragdb_data")
STALE_VOLUME_NAMES = ("olapdb_data", "dsdb_data", "dbarena_olapdb_data", "dbarena_dsdb_data")


def _readme() -> str:
    return README_PATH.read_text(encoding="utf-8")


def _makefile_targets() -> set[str]:
    text = MAKEFILE_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"^([a-z][a-z0-9_-]*)\s*:", text, re.MULTILINE))


def _compose_services(path: Path) -> set[str]:
    services: set[str] = set()
    in_services = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_services = line.strip() == "services:"
            continue
        if in_services:
            match = re.fullmatch(r"  ([a-z][a-z0-9_-]*):\s*", line)
            if match:
                services.add(match.group(1))
    return services


def _env_defaults() -> dict[str, str]:
    defaults: dict[str, str] = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(r"([A-Z0-9_]+)=(.*)", line.strip())
        if match:
            defaults[match.group(1)] = match.group(2)
    return defaults


def _bash_blocks(doc: str) -> list[str]:
    return re.findall(r"```(?:bash|sh|shell)\n(.*?)```", doc, re.DOTALL)


def _compose_service_references(doc: str) -> set[str]:
    references: set[str] = set()
    command = re.compile(r"^\s*\$?\s*(?:docker compose|COMPOSE=? ?docker compose)([^\n]*)$", re.MULTILINE)
    action = re.compile(
        r"\b(?:up|run|exec|logs|build|stop|restart|pull)\b"
        r"(?:\s+(?:-[a-zA-Z-]+|--profile\s+\S+|--rm|-T|--force-recreate))*"
        r"\s+([a-z][a-z0-9_-]*)"
    )
    for line in doc.splitlines():
        match = command.match(line.strip())
        if match:
            found = action.search(match.group(1))
            if found:
                references.add(found.group(1))
    return references


def _make_references(doc: str) -> set[str]:
    return set(re.findall(r"\bmake\s+([a-z][a-z0-9_-]*)", doc))


def _referenced_repo_paths(doc: str) -> list[str]:
    top_level = {entry.name for entry in REPO_ROOT.iterdir()}
    suffixes = (".md", ".yml", ".yaml", ".example", ".sh", ".py", ".ts", ".tsx", ".json", ".toml")
    paths: list[str] = []
    context = ""
    for quoted in re.findall(r"`([^`\n]+)`", doc):
        tokens = re.findall(r"[A-Za-z0-9_./-]{2,}", quoted)
        for token in tokens:
            if ":" in token:
                continue
            if token.endswith("/"):
                if token.split("/")[0] in top_level:
                    context = token
                continue
            if "/" in token:
                if token.split("/")[0] in top_level:
                    paths.append(token)
                    context = ""
                continue
            if context and Path(token).suffix in suffixes:
                if (REPO_ROOT / token).exists() or not (REPO_ROOT / context / token).exists():
                    paths.append(token)
                else:
                    paths.append(context + token)
            elif Path(token).suffix in suffixes:
                paths.append(token)
    return paths


class TestReferencedCommands:
    def test_every_make_target_referenced_in_readme_exists(self):
        unknown = _make_references(_readme()) - _makefile_targets()
        assert not unknown, f"README references make targets that do not exist: {sorted(unknown)}"

    def test_makefile_targets_cover_the_documented_lifecycle(self):
        targets = _makefile_targets()
        expected = {"build", "up", "down", "status", "logs", "reset", "seed", "seed-olap", "seed-ds",
                    "seed-ragdb", "tasks", "smoke"}
        missing = expected - targets
        assert not missing, f"Makefile is missing lifecycle targets: {sorted(missing)}"

    def test_every_compose_service_referenced_in_readme_exists(self):
        services: set[str] = set()
        for path in COMPOSE_PATHS:
            services |= _compose_services(path)
        references = _compose_service_references(_readme())
        unknown = references - services
        assert not unknown, f"README runs compose services that do not exist: {sorted(unknown)}"

    def test_every_compose_file_flag_in_readme_exists(self):
        doc = _readme()
        missing = [
            path
            for path in re.findall(r"-f\s+(\S+\.ya?ml)", doc)
            if not (REPO_ROOT / path).is_file()
        ]
        assert not missing, f"README passes -f files that do not exist: {missing}"

    def test_referenced_repo_paths_exist(self):
        missing = [path for path in _referenced_repo_paths(_readme()) if not (REPO_ROOT / path).exists()]
        assert not missing, f"README references repo paths that do not exist: {missing}"


class TestDocumentedConfiguration:
    def test_documented_core_ports_match_env_example_defaults(self):
        doc = _readme()
        defaults = _env_defaults()
        problems = [
            f"{var} default {defaults[var]} not stated in README"
            for var in PORT_VARS[:5]
            if defaults.get(var) and defaults[var] not in doc
        ]
        assert not problems, problems

    def test_stale_volume_names_are_absent(self):
        doc = _readme()
        stale = [name for name in STALE_VOLUME_NAMES if name in doc]
        assert not stale, f"README mentions volume names that do not exist in docker-compose.yml: {stale}"


class TestTargetContract:
    def test_fenced_code_blocks_declare_a_language(self):
        doc = _readme()
        unnamed = []
        in_block = False
        for line in doc.splitlines():
            if not line.strip().startswith("```"):
                continue
            if not in_block:
                in_block = True
                if not line.strip()[3:].strip():
                    unnamed.append(line)
            else:
                in_block = False
        assert not unnamed, "README contains fenced blocks without a language tag"

    def test_all_documented_ports_match_env_example_defaults(self):
        doc = _readme()
        defaults = _env_defaults()
        problems = [
            f"{var} default {defaults[var]} not stated in README"
            for var in PORT_VARS
            if defaults.get(var) and defaults[var] not in doc
        ]
        assert not problems, problems

    def test_stack_components_are_all_documented(self):
        doc = _readme()
        undocumented = [service for service in DOCUMENTED_SERVICES if service not in doc]
        assert not undocumented, f"README does not document services: {undocumented}"

    def test_named_volumes_are_documented(self):
        doc = _readme()
        missing = [volume for volume in NAMED_VOLUMES if volume not in doc]
        assert not missing, f"README does not document named volumes: {missing}"

    def test_duckdb_fixture_paths_are_documented(self):
        doc = _readme()
        for fragment in ("data/olapdb", "data/dsdb", "olap-tpch.duckdb", "tpcds.duckdb"):
            assert fragment in doc, f"README does not document the DuckDB fixture path fragment: {fragment}"

    def test_gpu_overlay_is_documented(self):
        assert "docker-compose.embed-gpu.yml" in _readme()

    def test_sglang_device_override_is_documented(self):
        assert "SGLANG_DEVICE" in _readme()

    def test_embedding_model_identity_is_documented(self):
        doc = _readme()
        defaults = _env_defaults()
        assert defaults.get("EMBEDDING_MODEL") in doc
        assert defaults.get("EMBEDDING_DIM") in doc

    def test_embedding_endpoint_variables_are_documented(self):
        doc = _readme()
        missing = [var for var in EMBEDDING_VARS if var not in doc]
        assert not missing, f"README does not document embedding variables: {missing}"

    def test_embedding_endpoint_default_targets_the_in_stack_server(self):
        defaults = _env_defaults()
        assert defaults.get("EMBEDDING_BASE_URL") in _readme()

    def test_lifecycle_seed_targets_are_itemized(self):
        doc = _readme()
        for target in ("seed", "seed-olap", "seed-ds", "seed-ragdb", "tasks"):
            assert f"make {target}" in doc or f"`make {target}`" in doc, f"seed target not itemized: {target}"
