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

    def test_sync_job_unchanged_contract(self):
        s = job('sync')
        self.assertNotIn('adult_tpdb', s)
        self.assertNotIn('git_publish', s)


class Publisher(unittest.TestCase):
    def test_script_uses_autostash_and_retry(self):
        sh = (Path(__file__).resolve().parent.parent / 'scripts' / 'git_publish.sh').read_text()
        self.assertIn('--rebase --autostash', sh)
        self.assertIn('PUBLISH_RETRIES', sh)
        self.assertIn('::error', sh)
        self.assertIn('git add -A', sh)


if __name__ == '__main__':
    unittest.main(verbosity=2)
