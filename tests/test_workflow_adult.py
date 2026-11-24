"""Static checks of the adult-covers job in sync-meta.yml (no YAML library needed).
Guards the fix for run 44953033386: all outputs committed, customer export before
the commit, commit failures visible, sync job unaffected."""
import re
import unittest
from pathlib import Path

WF = (Path(__file__).resolve().parent.parent / '.github' / 'workflows' / 'sync-meta.yml').read_text(encoding='utf-8')


def job(name):
    m = re.search(r'^  ' + re.escape(name) + r':\n(.*?)(?=^  [A-Za-z0-9_#-]|\Z)', WF, re.S | re.M)
    assert m, name
    return m.group(1)


def steps(body):
    parts = re.split(r'^      - name: ', body, flags=re.M)[1:]
    return {p.split('\n', 1)[0].strip(): p for p in parts}


class AdultJob(unittest.TestCase):
    def setUp(self):
        self.body = job('adult-covers')
        self.steps = steps(self.body)
        self.names = list(self.steps)

    def test_job_never_fails_the_run(self):
        head = self.body.split('    steps:')[0]
        self.assertIn('continue-on-error: true', head)
        self.assertIn('needs: sync', head)

    def test_tpdb_step_stays_non_fatal(self):
        tp = next(v for k, v in self.steps.items() if 'TPDB' in k)
        self.assertIn('continue-on-error: true', tp)
        self.assertIn('${{ secrets.TPDB_API_KEY }}', tp)
        self.assertNotRegex(tp, r'echo[^\n]*TPDB_API_KEY')

    def test_export_runs_before_commit(self):
        exp = [k for k in self.names if 'Export' in k]
        self.assertTrue(exp, self.names)
        self.assertLess(self.names.index(exp[0]), self.names.index('Änderungen übernehmen'))
        self.assertIn('scripts/export_client.py --data-dir data', self.steps[exp[0]])

    def test_commit_step_visible_and_complete(self):
        c = self.steps['Änderungen übernehmen']
        self.assertNotIn('continue-on-error', c, 'a failed push must turn the job red')
        self.assertIn('scripts/git_publish.sh', c)
        for p in ('data/adult-tpdb.json', 'data/status.json', 'data/client-v1'):
            self.assertIn(p, c)
        self.assertNotRegex(c, r'git pull --rebase(?! --autostash)')

    def test_sync_job_does_not_run_adult_step(self):
        s = job('sync')
        self.assertNotIn('adult_tpdb', s)


ROOTWF = Path(__file__).resolve().parent.parent / '.github' / 'workflows'


class AllPushingWorkflows(unittest.TestCase):
    """Run 45006009953: a queued sync started from the trigger commit (stale) and its plain
    `git pull --rebase` hit a status.json conflict with the adult job -> TMDB results lost."""

    def test_sync_checks_out_main_and_publishes_robustly(self):
        s = job('sync')
        self.assertRegex(s, r'uses: actions/checkout@v4\n\s+with:\n\s+ref: main')
        c = steps(s)['Änderungen übernehmen']
        self.assertIn('if: always()', c)
        self.assertIn('scripts/git_publish.sh', c)
        self.assertNotIn('continue-on-error', c)
        for p in ('data/index.json', 'data/status.json', 'data/adult-titles.json', 'data/meta-*.json', 'git add -u data/'):
            self.assertIn(p, c)

    def test_no_plain_pull_rebase_left(self):
        for f in ROOTWF.glob('*.yml'):
            t = f.read_text(encoding='utf-8')
            self.assertNotRegex(t, r'git pull --rebase(?! --autostash)', f.name)
            self.assertNotRegex(t, r'\n\s+git push', f.name)

    def test_every_pushing_workflow_uses_publisher_and_main(self):
        for name in ('sync-meta.yml', 'export-client.yml', 'sync-sport.yml'):
            t = (ROOTWF / name).read_text(encoding='utf-8')
            self.assertIn('scripts/git_publish.sh', t, name)
            self.assertRegex(t, r'uses: actions/checkout@v4\n\s+with:\n\s+ref: main', name)

    def test_export_rebuilds_on_conflict(self):
        t = (ROOTWF / 'export-client.yml').read_text(encoding='utf-8')
        self.assertIn('PUBLISH_REGEN: python3 scripts/export_client.py --data-dir data', t)
        self.assertIn('data/client-v1', t)

    def test_sport_publishes_only_sport_json(self):
        t = (ROOTWF / 'sync-sport.yml').read_text(encoding='utf-8')
        self.assertRegex(t, r'git_publish\.sh "chore: Sportplan aktualisieren" data/sport\.json\n')

    def test_regression_tests_watch_all_workflows(self):
        t = (ROOTWF / 'test-metadata.yml').read_text(encoding='utf-8')
        for name in ('sync-meta.yml', 'export-client.yml', 'sync-sport.yml'):
            self.assertIn('.github/workflows/' + name, t)


class Publisher(unittest.TestCase):
    def test_script_uses_autostash_and_retry(self):
        sh = (Path(__file__).resolve().parent.parent / 'scripts' / 'git_publish.sh').read_text()
        self.assertIn('--rebase --autostash', sh)
        self.assertIn('PUBLISH_RETRIES', sh)
        self.assertIn('::error', sh)
        self.assertIn('git add -A', sh)
        self.assertIn('PUBLISH_REGEN', sh)


class AdultOnlyMode(unittest.TestCase):
    """workflow_dispatch mode=adult skips TMDB and still runs adult-covers."""

    def test_dispatch_mode_input_defaults_to_full(self):
        # Manager posts only {"ref":"main"}; GitHub then applies default: full.
        self.assertRegex(
            WF,
            re.compile(
                r"workflow_dispatch:\s+inputs:\s+mode:.*?type: choice.*?options:\s+- full\s+- adult\s+default: full",
                re.S,
            ),
        )

    def test_sync_skipped_only_for_adult_mode(self):
        head = job("sync").split("    steps:")[0]
        self.assertIn(
            "github.event_name != 'workflow_dispatch' || github.event.inputs.mode != 'adult'",
            head,
        )
        # schedule and a dispatch without inputs / mode=full must still run sync
        self.assertNotIn("event_name == 'schedule'", head)

    def test_adult_covers_runs_after_skipped_or_failed_sync(self):
        head = job("adult-covers").split("    steps:")[0]
        self.assertIn("needs: sync", head)
        self.assertIn("if: ${{ always() && !cancelled() }}", head)

    def test_concurrency_group_unchanged(self):
        self.assertRegex(WF, r"concurrency:\s+group: sync-meta\s+cancel-in-progress: false")


if __name__ == '__main__':
    unittest.main(verbosity=2)
