import plistlib
from pathlib import Path

DEPLOY = Path(__file__).parent.parent / "deploy"


def test_launchd_job_runs_paper_every_15_minutes():
    job = plistlib.loads((DEPLOY / "com.weatherbot.run.plist").read_bytes())
    assert job["StartInterval"] == 900
    assert job["ProgramArguments"][1:] == ["run"]


def test_cron_example_runs_paper_every_15_minutes():
    lines = (DEPLOY / "crontab.example").read_text(encoding="utf8").splitlines()
    jobs = [line for line in lines if line and not line.startswith("#")]
    assert len(jobs) == 1
    assert jobs[0].startswith("*/15 * * * * ")
    assert "weatherbot run >>" in jobs[0]


def test_nothing_scheduled_goes_live():
    for path in DEPLOY.iterdir():
        assert "--live" not in path.read_text(encoding="utf8"), path.name
