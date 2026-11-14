"""First-wave line observations and push contract regression coverage."""
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dyro.config import load, Policy, push_policy_fields, PUSH_DISABLED_NOTE
from dyro.instructions import render_workspace_overlay, render_line_persona
from dyro.read_limits import ReadBudget, ObservationLimits, ReadLimitCode, ReadLimitError
from dyro.workspace import (Line, _write_line, _parse_line_content, next_line_context,
                            line_repository_path, _overlay_warnings)
from dyro.continuation.resolution import next_directory_location
from .support import WorkspaceCase


class LineNextContractTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.config = load(self.root)
        self.line = Line('core', 'line', 'feat/core', 'main', ('api',))

    def budget(self):
        return ReadBudget(ObservationLimits())

    def test_contract_exact_and_enabled(self):
        for policy in (Policy(), Policy(allow_push=True)):
            fields = push_policy_fields(policy)
            self.assertEqual(fields['push_note'], PUSH_DISABLED_NOTE)
            self.assertEqual(fields['push_contract_version'], 1)
        for body in (render_workspace_overlay(), render_line_persona(line_id='core', branch='feat/core')):
            self.assertIn(PUSH_DISABLED_NOTE, body)
        skill = Path(__file__).resolve().parents[1] / 'src/dyro/integrations/assets/dyro-control-plane/SKILL.md'
        self.assertIn(PUSH_DISABLED_NOTE, skill.read_text(encoding='utf-8'))

    def test_manifest_anchors_roundtrip_and_old_schema_rejection(self):
        for parent in ('', 'parent'):
            line = replace(self.line, parent=parent, remote_anchors={'api': 'a'*40})
            _write_line(self.config, line)
            path = self.config.lines_state_dir / 'core.toml'
            content = path.read_bytes()
            self.assertIn(b'schema_version = 4', content)
            self.assertEqual(_parse_line_content(path, content), line)
            with self.assertRaises(Exception):
                _parse_line_content(path, content.replace(b'schema_version = 4', b'schema_version = 3'))
        _write_line(self.config, self.line)
        self.assertIn(b'schema_version = 2', path.read_bytes())

    def test_directory_limits_keep_completed_matches(self):
        for code in ReadLimitCode:
            budget = self.budget()
            budget._doctor_lines = [self.line]
            with patch('dyro.tasks.list_task_ids_bounded', side_effect=ReadLimitError(code, 'limit')):
                location, target, _, limited = next_directory_location(self.config, self.root / 'versions/core', budget)
            self.assertEqual(limited, code)
            self.assertEqual(location['cwd_lines'], ['core'])
            self.assertEqual(location['reason'], 'task_scan_incomplete')
            self.assertIsNone(target)
            with patch('dyro.continuation.resolution.list_lines', side_effect=ReadLimitError(code, 'limit')):
                location, target, _, _ = next_directory_location(self.config, self.root, self.budget())
            self.assertEqual(location['reason'], 'line_resolution_limited')
            self.assertIsNone(target)

    def test_location_flag_outside_and_ambiguity(self):
        budget = self.budget()
        other = replace(self.line, id='other')
        budget._doctor_lines = [self.line, other]
        with patch('dyro.tasks.list_task_ids_bounded', return_value=()):
            location, target, _, _ = next_directory_location(self.config, self.root / 'versions/core', budget, line='other')
            self.assertTrue(location['disagree'])
            self.assertEqual(target, other)
            location, _, _, _ = next_directory_location(self.config, self.root.parent, budget, explicit_workspace=True)
            self.assertEqual(location['reason'], 'cwd_outside_selected_workspace')

    def test_upstream_states_and_same_sha_avoid_process(self):
        for upstream, head, remote, expected in (
            ('', 'a'*40, 'a'*40, 'same_sha_unconfigured'),
            ('origin/feat/core', 'a'*40, 'a'*40, 'ok'),
            ('origin/other', 'a'*40, 'a'*40, 'mismatch'),
            ('', '', 'a'*40, 'unknown'),
            ('', 'a'*40, None, 'missing'),
        ):
            budget = self.budget()
            budget._doctor_complete = True
            budget._doctor_observed = {'api': ({line_repository_path(self.config, self.line, 'api').resolve(): (head, self.line.branch)}, {self.line.branch: upstream}, {} if remote is None else {'refs/remotes/origin/feat/core': remote})}
            with patch('dyro.workspace.git_read') as git:
                context, limited = next_line_context(self.config, self.line, [self.line], budget)
                git.assert_not_called()
            self.assertIsNone(limited)
            self.assertEqual(context['repositories'][0]['upstream_state'], expected)
            self.assertEqual(context['push_ready'], None if expected == 'unknown' else expected in ('ok', 'same_sha_unconfigured'))

    def test_overlay_local_degrade_and_global_reraise(self):
        path = self.root / 'AGENTS.md'
        path.write_text('custom')
        for code in ReadLimitCode:
            budget = self.budget()
            with patch.object(budget, 'read_regular_bytes', side_effect=ReadLimitError(code, 'limit')):
                if code in (ReadLimitCode.FILE_TOO_LARGE, ReadLimitCode.UNSAFE_FILE):
                    findings = _overlay_warnings(self.root, 'expected', 'workspace', budget)
                    self.assertEqual(len(findings), 1)
                    self.assertIn('无法核对', findings[0])
                else:
                    with self.assertRaises(ReadLimitError):
                        _overlay_warnings(self.root, 'expected', 'workspace', budget)
        self.assertEqual(path.read_text(), 'custom')

    def test_unconfigured_counts_cap_failures_and_limit_codes(self):
        from unittest.mock import Mock
        repos = tuple(f'repo{i}' for i in range(5))
        config = replace(self.config, repositories={r: replace(next(iter(self.config.repositories.values())), id=r) for r in repos})
        line = replace(self.line, repositories=repos)
        def observed_budget():
            budget = self.budget()
            budget._doctor_complete = True
            budget._doctor_observed = {r: ({line_repository_path(config, line, r).resolve(): ('b'*40, line.branch)}, {}, {'refs/remotes/origin/feat/core': 'a'*40}) for r in repos}
            return budget
        with patch('dyro.workspace.git_read', return_value=Mock(code=0, stdout='2 3')) as git:
            context, limited = next_line_context(config, line, [line], observed_budget())
        self.assertEqual(git.call_count, 4)
        self.assertIsNone(limited)
        self.assertFalse(context['push_ready'])
        self.assertEqual(context['repositories'][0]['upstream_state'], 'unconfigured')
        self.assertEqual(context['repositories'][0]['unpushed_commits'], 3)
        self.assertEqual(context['repositories'][0]['behind'], 2)
        self.assertIsNone(context['repositories'][4]['ahead'])
        self.assertTrue(context['repositories'][4]['counts_omitted'])
        for code in ReadLimitCode:
            with patch('dyro.workspace.git_read', side_effect=ReadLimitError(code, 'limit')) as git:
                context, limited = next_line_context(config, line, [line], observed_budget())
            self.assertEqual(limited, code)
            self.assertEqual(git.call_count, 1)
            self.assertTrue(all(x['ahead'] is None for x in context['repositories']))
        budget = observed_budget()
        with patch.object(budget, 'remaining_seconds', return_value=0.5), patch('dyro.workspace.git_read') as git:
            context, limited = next_line_context(config, line, [line], budget)
            git.assert_not_called()
        self.assertIsNone(limited)
        self.assertTrue(all(x["ahead"] is None and x["behind"] is None and x["unpushed_commits"] is None for x in context["repositories"]))
        with patch('dyro.workspace.git_read', return_value=Mock(code=1, stdout='')):
            context, limited = next_line_context(config, line, [line], observed_budget())
        self.assertIsNone(context['repositories'][0]['ahead'])

    def test_anchor_reference_and_children(self):
        line = replace(self.line, storage_modes={'api': 'anchor-reference'})
        child = replace(self.line, id='child', parent=line.id)
        with patch('dyro.workspace.git_read') as git:
            context, _ = next_line_context(self.config, line, [line, child], self.budget())
            git.assert_not_called()
        self.assertIsNone(context['push_ready'])
        self.assertEqual(context['repositories'][0]['upstream_state'], 'not_applicable')
        self.assertEqual(context['children'], ['child'])
        self.assertNotIn('siblings', context)
        self.assertNotIn('preview', context)

    def test_json_directory_envelopes_and_text_success(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        from dyro.cli import main
        from dyro.workspace import create_line
        create_line(self.config, line_id='core', branch='feat/core', base='main')
        for code in ReadLimitCode:
            for fmt in ('json', 'text'):
                output = StringIO()
                with patch('dyro.tasks.list_task_ids_bounded', side_effect=ReadLimitError(code, 'limit')), redirect_stdout(output):
                    if fmt == 'json':
                        with self.assertRaises(SystemExit) as caught:
                            main(['--root', str(self.root), 'next', '--format', fmt])
                        self.assertEqual(caught.exception.code, 2)
                    else:
                        main(['--root', str(self.root), 'next', '--format', fmt])
                if fmt == 'json':
                    payload = json.loads(output.getvalue())
                    self.assertEqual(payload['kind'], 'next_step')
                    self.assertEqual(payload['code'], code.value)
                    self.assertTrue(payload['partial'])
                    self.assertIsNone(payload['line_context'])
                    self.assertEqual(payload['mutation_unavailable_reason'], 'partial_observation')
                    self.assertEqual(payload['commands'], [])
                else:
                    self.assertNotIn('当前线', output.getvalue())
                    self.assertNotIn('dyro start', output.getvalue())

    def test_successful_push_anchor_survives_later_push_failure(self):
        from unittest.mock import Mock
        from dyro.workspace import _LineMergePlan, _merge_line_repositories_locked, get_line
        target = replace(self.line, repositories=('api', 'web'))
        plans = tuple(_LineMergePlan(r, self.root / r, 'a'*40, 'b'*40) for r in target.repositories)
        def fake_git(path, *argv, **kwargs):
            if argv[:2] == ('rev-parse', '--verify'):
                return Mock(code=1, stdout='')
            if argv[0] == 'push' and path.name == 'web':
                return Mock(code=1, stdout='rejected')
            return Mock(code=0, stdout='')
        with patch('dyro.workspace._prepare_line_merge_plans', return_value=plans), patch('dyro.workspace.git', side_effect=fake_git), patch('dyro.workspace.git_read', return_value=Mock(code=0, stdout='c'*40)), patch('dyro.workspace._line_ledger'):
            with self.assertRaisesRegex(Exception, '推送 web 失败'):
                _merge_line_repositories_locked(self.config, target=target, source=self.line,
                    repositories=target.repositories, message='merge', phase='line_merge', push=True, dry_run=False)
        loaded = get_line(self.config, target.id)
        self.assertEqual(loaded.remote_anchors, {'api': 'c'*40})
        self.assertIn('schema_version = 4', (self.config.lines_state_dir / 'core.toml').read_text())

    def test_task_directory_hit_precedes_outside(self):
        from unittest.mock import Mock
        budget = self.budget()
        budget._doctor_lines = [self.line]
        task = Mock(line=self.line.id)
        outside = self.root.parent / 'external-task'
        with patch('dyro.tasks.list_task_ids_bounded', return_value=('T',)), patch('dyro.tasks.load_task_bounded', return_value=task) as load_task, patch('dyro.continuation.resolution.worktree_root', return_value=outside):
            location, target, _, limited = next_directory_location(self.config, outside, budget, explicit_workspace=True)
        self.assertEqual(target, self.line)
        self.assertEqual(location['cwd_lines'], ['core'])
        self.assertNotEqual(location['reason'], 'cwd_outside_selected_workspace')
        self.assertEqual(load_task.call_args.kwargs['known_line_ids'], frozenset({'core'}))

    def test_real_unconfigured_next_remains_repair_with_counts(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        from dyro.cli import main
        from dyro.workspace import create_line
        from .support import shell
        line = create_line(self.config, line_id='core', branch='feat/core', base='main')
        # A local remote-tracking object is enough: no fetch or remote access.
        shell('git', 'update-ref', 'refs/remotes/origin/feat/core', 'HEAD', cwd=self.anchor)
        path = line_repository_path(self.config, line, 'api')
        shell('git', 'commit', '--allow-empty', '-m', 'local', cwd=path)
        output = StringIO()
        with redirect_stdout(output), patch('pathlib.Path.cwd', return_value=path):
            main(['--root', str(self.root), 'next', '--format', 'json'])
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['state'], 'needs_repair')
        self.assertEqual(payload['mutation_unavailable_reason'], 'repair_not_applicable')
        self.assertFalse(payload['line_context']['push_ready'])
        repo = payload['line_context']['repositories'][0]
        self.assertEqual(repo['upstream_state'], 'unconfigured')
        self.assertEqual((repo['ahead'], repo['behind'], repo['unpushed_commits']), (1, 0, 1))
        self.assertFalse(payload.get('partial', False))
        self.assertTrue(any('expected upstream origin/feat/core, found -' in x['message'] for x in payload['findings']))

    def test_interactive_ready_start_only_at_workspace_level(self):
        from contextlib import redirect_stdout
        from io import StringIO
        from dyro.cli import main
        from dyro.workspace import create_line
        create_line(self.config, line_id='core', branch='feat/core', base='main')
        class TtyOutput(StringIO):
            def isatty(self):
                return True
        for cwd, start_expected in ((self.root, True), (self.root / 'versions/core', False)):
            output = TtyOutput()
            with redirect_stdout(output), patch('sys.stdin.isatty', return_value=True), patch('pathlib.Path.cwd', return_value=cwd):
                main(['--root', str(self.root), 'next'])
            self.assertEqual('dyro start' in output.getvalue(), start_expected)

    def test_status_push_ready_uses_storage_without_more_git(self):
        from dyro.cli import _status_payload
        line = replace(self.line, repositories=('api', 'web'), storage_modes={'web': 'anchor-reference'})
        budget = self.budget()
        budget._status_lines = [line]
        rows = [('line:core', 'api', line.branch, 'sha', 'origin/feat/core', 0),
                ('line:core', 'web', line.branch, 'sha', 'origin/main', 0)]
        with patch('dyro.cli.status_rows', return_value=rows), patch('dyro.workspace.git_read') as git:
            payload = _status_payload(self.config, read_budget=budget)
            git.assert_not_called()
        self.assertEqual(payload['line_push_ready'], [dict(id='core', kind='line', push_ready=True)])
        rows[0] = ('line:core', 'api', line.branch, 'sha', '-', 0)
        with patch('dyro.cli.status_rows', return_value=rows):
            self.assertFalse(_status_payload(self.config, read_budget=budget)['line_push_ready'][0]['push_ready'])
        budget._status_lines = [replace(line, storage_modes={'api': 'anchor-reference', 'web': 'anchor-reference'})]
        with patch('dyro.cli.status_rows', return_value=rows):
            self.assertIsNone(_status_payload(self.config, read_budget=budget)['line_push_ready'][0]['push_ready'])

    def test_doctor_overlay_warnings_do_not_modify_and_stop_on_aggregate(self):
        from dyro.workspace import doctor
        from dyro.instructions import seed_workspace_overlay
        seed_workspace_overlay(self.root)
        path = self.root / 'AGENTS.md'
        path.write_text('custom')
        findings = doctor(self.config, read_budget=self.budget())
        self.assertEqual(sum('workspace AGENTS.md 与当前模板不一致' in x for x in findings), 1)
        self.assertEqual(path.read_text(), 'custom')
        budget = self.budget()
        original = budget.read_regular_bytes
        def read(path, **kwargs):
            if path.name == 'AGENTS.md':
                raise ReadLimitError(ReadLimitCode.AGGREGATE_BYTES_EXCEEDED, 'limit')
            return original(path, **kwargs)
        with patch.object(budget, 'read_regular_bytes', side_effect=read), patch('dyro.workspace.list_lines') as lines, patch('dyro.workspace._worktree_inventory') as git:
            with self.assertRaises(ReadLimitError):
                doctor(self.config, read_budget=budget)
            lines.assert_not_called()
            git.assert_not_called()
        self.assertTrue(getattr(budget, '_control_plane_findings'))

    def test_task_limit_preserves_needs_line_mutation(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        from dyro.cli import main
        output = StringIO()
        with patch('dyro.tasks.list_task_ids_bounded', side_effect=ReadLimitError(ReadLimitCode.RECORD_LIMIT_EXCEEDED, 'limit')), redirect_stdout(output):
            with self.assertRaises(SystemExit) as caught:
                main(['--root', str(self.root), 'next', '--format', 'json'])
        self.assertEqual(caught.exception.code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['state'], 'needs_line')
        self.assertTrue(payload['mutation_available'])
        self.assertNotIn('mutation_unavailable_reason', payload)
        self.assertNotIn('line_context', payload)

    def test_line_overlay_limits_precede_manifest_and_inventory_reads(self):
        from dyro.workspace import doctor
        directory = self.root / 'versions/core'
        directory.mkdir(parents=True)
        (directory / 'AGENTS.md').write_text('custom')
        for code in (ReadLimitCode.AGGREGATE_BYTES_EXCEEDED, ReadLimitCode.RECORD_LIMIT_EXCEEDED):
            budget = self.budget()
            with patch.object(budget, 'read_regular_bytes', side_effect=ReadLimitError(code, 'limit')), patch('dyro.workspace.list_lines') as manifests, patch('dyro.workspace._worktree_inventory') as inventory:
                with self.assertRaises(ReadLimitError) as caught:
                    doctor(self.config, read_budget=budget)
            self.assertEqual(caught.exception.code, code)
            manifests.assert_not_called()
            inventory.assert_not_called()
            self.assertTrue(budget._control_plane_findings)

    def test_incomplete_ref_inventory_is_unknown(self):
        from dyro.workspace import doctor, create_line
        line = create_line(self.config, line_id='core', branch='feat/core', base='main')
        budget = self.budget()
        with patch('dyro.workspace._ref_inventory', side_effect=ReadLimitError(ReadLimitCode.DEADLINE_EXCEEDED, 'limit')):
            doctor(self.config, read_budget=budget)
        context, limited = next_line_context(self.config, line, [line], budget)
        self.assertEqual(context['repositories'][0]['upstream_state'], 'unknown')
        self.assertIsNone(context['push_ready'])
        self.assertIsNone(limited)

    def test_root_outside_is_not_explicit_workspace(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import json
        from dyro.cli import main
        output = StringIO()
        with redirect_stdout(output), patch('pathlib.Path.cwd', return_value=self.root.parent):
            main(['--root', str(self.root), 'next', '--format', 'json'])
        self.assertEqual(json.loads(output.getvalue())['location']['reason'], 'cwd_not_in_line')

    def test_stale_task_does_not_discard_healthy_line_match(self):
        from dyro.tasks import task_template
        from dyro.workspace import _write_line
        _write_line(self.config, self.line)
        directory = self.config.task_specs_dir / 'stale'
        directory.mkdir(parents=True)
        (directory / 'task.toml').write_text(task_template('stale', 'obsolete', 'deleted', 'api', 'api'))
        location, target, _, limited = next_directory_location(self.config, self.root / 'versions/core', self.budget())
        self.assertEqual(location['cwd_lines'], ['core'])
        self.assertEqual(target, self.line)
        self.assertIsNone(limited)
        location, target, _, _ = next_directory_location(self.config, self.root / self.config.layout.tasks / 'deleted/stale', self.budget())
        self.assertEqual(location['cwd_lines'], [])
        self.assertIsNone(target)

    def test_line_persona_and_dangling_overlay_missing_predicate(self):
        from dyro.instructions import missing_workspace_overlay_files
        self.assertNotIn('不要绕过门禁 merge/push', render_line_persona(line_id='core', branch='feat/core'))
        (self.root / 'AGENTS.md').symlink_to(self.root / 'absent')
        (self.root / 'CLAUDE.md').mkdir()
        self.assertEqual(missing_workspace_overlay_files(self.root), ('AGENTS.md', 'CLAUDE.md'))
