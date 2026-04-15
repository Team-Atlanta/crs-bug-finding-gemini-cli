"""
Gemini CLI agent for autonomous vulnerability discovery.

Implements the agent interface (setup / run) using Gemini CLI
in agentic mode. Gemini reads GEMINI.md for workflow instructions,
then autonomously: analyzes source -> identifies vulnerabilities ->
crafts inputs -> verifies crashes -> writes POVs to pov_dir/.
"""

import logging
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("agent.gemini_cli")

_raw_model = os.environ.get("GEMINI_MODEL", "gemini-3-pro-preview").strip()
GEMINI_MODEL = _raw_model.removeprefix("gemini/").removeprefix("google/")

# 0 = no timeout (run until budget is exhausted)
try:
    AGENT_TIMEOUT = int(os.environ.get("AGENT_TIMEOUT", "0"))
except ValueError:
    AGENT_TIMEOUT = 0
if AGENT_TIMEOUT < 0:
    AGENT_TIMEOUT = 0

_TEMPLATE_PATH = Path(__file__).with_name("GEMINI.md")
_SECTIONS_DIR = _TEMPLATE_PATH.with_name("sections")
_SKILLS_DIR = _TEMPLATE_PATH.with_name("skills")


def _load_section(section_name: str) -> str:
    section_path = _SECTIONS_DIR / section_name
    return section_path.read_text()


def _load_prompt_templates() -> dict[str, str]:
    return {
        "agents_md": _TEMPLATE_PATH.read_text(),
        "workflow_find": _load_section("workflow_find.md"),
        "diff_present": _load_section("diff_present.md"),
        "diff_absent": _load_section("diff_absent.md"),
        "seeds_present": _load_section("seeds_present.md"),
        "pre_submit": _load_section("pre_submit.md"),
    }


def _md_inline(value: str) -> str:
    """Return a markdown-safe inline code span."""
    ticks = 1
    while "`" * ticks in value:
        ticks += 1
    fence = "`" * ticks
    return f"{fence}{value}{fence}"


def _list_input_files(input_dir: Path, *, non_empty_only: bool = False) -> list[Path]:
    if not input_dir.exists():
        return []
    files = sorted(
        f for f in input_dir.rglob("*") if f.is_file() and not f.name.startswith(".")
    )
    if not non_empty_only:
        return files
    return [f for f in files if f.read_text(errors="replace").strip()]


def _install_skills(source_dir: Path, harness: str) -> None:
    """Copy skills from package data into source_dir/.agents/skills/."""
    target_skills = source_dir / ".agents" / "skills"
    if not _SKILLS_DIR.exists():
        logger.warning("Skills directory not found: %s", _SKILLS_DIR)
        return

    for skill_dir in _SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        destination = target_skills / skill_dir.name
        if destination.exists():
            shutil.rmtree(destination)
        shutil.copytree(skill_dir, destination)
        # Fill {harness}, {source_dir} placeholders in SKILL.md
        skill_md = destination / "SKILL.md"
        if skill_md.exists():
            content = skill_md.read_text()
            content = content.replace("{harness}", harness)
            content = content.replace("{source_dir}", str(source_dir))
            skill_md.write_text(content)
        logger.info("Installed skill: %s", skill_dir.name)


def setup(source_dir: Path, config: dict) -> None:
    """One-time agent configuration."""
    try:
        version_result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        logger.info(
            "Gemini CLI version: %s",
            version_result.stdout.strip() or version_result.stderr.strip(),
        )
    except OSError as error:
        logger.warning("Failed to get Gemini CLI version: %s", error)

    llm_api_url = config.get("llm_api_url", "")
    llm_api_key = config.get("llm_api_key", "")
    gemini_home = Path(config.get("gemini_home", Path.home() / ".gemini"))
    gemini_home.mkdir(parents=True, exist_ok=True)

    os.environ["IS_SANDBOX"] = "1"
    os.environ["GEMINI_CLI_HOME"] = str(gemini_home.parent)
    os.environ["GEMINI_SANDBOX"] = "false"
    os.environ["GEMINI_MODEL"] = GEMINI_MODEL

    if llm_api_url and llm_api_key:
        os.environ["GOOGLE_GEMINI_BASE_URL"] = llm_api_url
        os.environ["GEMINI_API_KEY"] = llm_api_key
        logger.info("Gemini CLI configured with LiteLLM proxy: %s", llm_api_url)
        logger.info("GEMINI_MODEL: %s", GEMINI_MODEL)
    else:
        logger.warning("No LLM API URL/key set, Gemini CLI may not work")

    settings_path = gemini_home / "settings.json"
    settings_path.write_text("{}\n")
    settings_path.chmod(0o600)
    logger.info("Wrote Gemini CLI settings to %s", settings_path)

    global_gitignore = Path.home() / ".gitignore"
    existing = ""
    if global_gitignore.exists():
        existing = global_gitignore.read_text(errors="replace")
    lines = [line.rstrip("\n") for line in existing.splitlines()]
    if "GEMINI.md" not in lines:
        lines.append("GEMINI.md")
    global_gitignore.write_text("\n".join(lines).rstrip("\n") + "\n")
    try:
        git_config = subprocess.run(
            ["git", "config", "--global", "core.excludesFile", str(global_gitignore)],
            capture_output=True,
        )
        if git_config.returncode != 0:
            logger.warning(
                "Failed to set global git excludesFile: %s",
                git_config.stderr.decode(errors="replace")
                if isinstance(git_config.stderr, bytes)
                else git_config.stderr,
            )
    except OSError as error:
        logger.warning("Failed to run git config for excludesFile: %s", error)

    logger.info("Agent setup complete")


def run(
    source_dir: Path,
    build_dir: Path,
    pov_dir: Path,
    diff_dir: Path,
    seed_dir: Path,
    bug_candidate_dir: Path,
    harness: str,
    work_dir: Path,
    *,
    language: str = "c",
    sanitizer: str = "address",
) -> bool:
    """Launch Gemini CLI in agentic mode to autonomously find vulnerabilities."""
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        templates = _load_prompt_templates()
    except OSError as error:
        logger.error("Failed to load prompt template(s): %s", error)
        return False

    _install_skills(source_dir, harness)

    diffs = _list_input_files(diff_dir, non_empty_only=True)
    seeds = _list_input_files(seed_dir)
    bug_candidates = _list_input_files(bug_candidate_dir)

    if diffs:
        diff_list = "\n".join(f"- {_md_inline(str(path))}" for path in diffs)
        diff_section = templates["diff_present"].format(diff_list=diff_list)
    else:
        diff_section = templates["diff_absent"]

    if seeds:
        seed_list = "\n".join(f"- {_md_inline(str(path))}" for path in seeds)
        seed_section = templates["seeds_present"].format(seed_list=seed_list)
    else:
        seed_section = ""

    if bug_candidates:
        bug_candidate_list = "\n".join(
            f"- {_md_inline(str(path))}" for path in bug_candidates
        )
        bug_candidate_section = (
            "## Bug-Candidate Reports\n\n"
            "Static analysis reports are available:\n\n"
            f"{bug_candidate_list}\n\n"
            "Use these to prioritize which code paths to target.\n"
        )
    else:
        bug_candidate_section = ""

    gemini_md = templates["agents_md"].format(
        language=language,
        sanitizer=sanitizer,
        source_dir=source_dir,
        build_dir=build_dir,
        work_dir=work_dir,
        harness=harness,
        pov_dir=pov_dir,
        workflow_section=templates["workflow_find"],
        diff_section=diff_section,
        seed_section=seed_section,
        bug_candidate_section=bug_candidate_section,
        pre_submit_section=templates["pre_submit"],
    )
    (source_dir / "GEMINI.md").write_text(gemini_md)

    target = os.environ.get("OSS_CRS_TARGET", source_dir.name)
    prompt_lines = [
        f"Find vulnerabilities in project {_md_inline(target)} through harness {_md_inline(harness)}.",
        f"Write crashing inputs (POVs) to {_md_inline(str(pov_dir))}.",
        "",
        "Available evidence:",
        f"- Diff files: {len(diffs)}",
        f"- Seed files: {len(seeds)}",
        f"- Bug-candidate files: {len(bug_candidates)}",
    ]
    if diffs:
        diff_files = " ".join(_md_inline(str(path)) for path in diffs)
        prompt_lines.append(f"- Diff files: {diff_files}")
    if seeds:
        seed_files = " ".join(_md_inline(str(path)) for path in seeds)
        prompt_lines.append(f"- Seed files: {seed_files}")
    if bug_candidates:
        bug_files = " ".join(_md_inline(str(path)) for path in bug_candidates)
        prompt_lines.append(f"- Bug-candidate report files: {bug_files}")
    prompt_lines.extend(
        [
            "",
            "Read GEMINI.md for workflow, environment, and submission instructions.",
            "Keep going until killed and find as many distinct vulnerabilities as possible.",
        ]
    )
    prompt = "\n".join(prompt_lines)

    stdout_log = work_dir / "gemini_stdout.log"
    stderr_log = work_dir / "gemini_stderr.log"
    cmd = [
        "gemini",
        "-m",
        GEMINI_MODEL,
        "--approval-mode",
        "yolo",
        "-p",
        prompt,
    ]

    (work_dir / "agent_prompt.txt").write_text(prompt)
    (work_dir / "agent_gemini_md.md").write_text(gemini_md)
    (work_dir / "agent_cmd.txt").write_text(" ".join(cmd) + "\n")
    logger.info("Agent inputs saved to %s", work_dir)

    try:
        with open(stdout_log, "w") as stdout_file, open(stderr_log, "w") as stderr_file:
            proc = subprocess.Popen(
                cmd,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                cwd=source_dir,
                start_new_session=True,
            )
            try:
                proc.wait(timeout=AGENT_TIMEOUT or None)
                logger.info("Gemini CLI exit code: %d", proc.returncode)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Gemini CLI timed out (%ds), killing process tree", AGENT_TIMEOUT
                )
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(2)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                logger.info("Gemini CLI exit code after timeout handling: %d", proc.returncode)
    except Exception as error:
        logger.error("Error running Gemini CLI: %s", error)
        return False

    gemini_tmp_bin = Path.home() / ".gemini" / "tmp" / "bin"
    if gemini_tmp_bin.is_dir():
        shutil.rmtree(gemini_tmp_bin, ignore_errors=True)
        logger.info("Cleaned up Gemini tmp/bin dir to reduce log artifact size")

    subprocess.run(
        ["chmod", "-R", "og+rX", str(Path.home() / ".gemini")],
        capture_output=True,
    )

    if proc.returncode != 0:
        logger.warning("Gemini CLI failed (rc=%d), see %s", proc.returncode, stderr_log)

    pov_files = list(pov_dir.glob("*")) if pov_dir.exists() else []
    pov_files = [path for path in pov_files if path.is_file() and not path.name.startswith(".")]
    if pov_files:
        logger.info(
            "Agent produced %d POV(s): %s", len(pov_files), [path.name for path in pov_files]
        )
        return True

    logger.info("Agent did not produce any POVs")
    return False
