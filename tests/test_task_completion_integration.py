"""Read each region's observed completion badge before doing local work."""

import importlib.util
import unittest
from unittest.mock import Mock, patch

import test_browser_integration as integration


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright 未安装")
class TaskCompletionIntegrationTests(unittest.TestCase):
    setUpClass = integration.BrowserIntegrationTests.__dict__["setUpClass"]
    tearDownClass = integration.BrowserIntegrationTests.__dict__["tearDownClass"]
    setUp = integration.BrowserIntegrationTests.setUp
    launch = integration.BrowserIntegrationTests.launch

    def metrics(self):
        return self.browser.page.evaluate(
            "JSON.parse(localStorage.getItem('task-completion-metrics'))"
        )

    def launch_tasks(self, layout):
        self.launch(
            f"task_completion.html?chapterId=local-completion&courseId=local&layout={layout}"
        )
        self.browser.page.wait_for_function(
            "window.completionFixtureReady && window.completionFixtureReady()"
        )
        self.assertIn(self.browser.prepare_start(), {"document", "video"})

    def run_tasks(self, layout, initial_completed, executed_ids, executed_kinds):
        from gaybar_tools.workflow import Workflow

        self.launch_tasks(layout)
        tasks = self.browser.content_tasks()
        self.assertEqual([task.completed for task in tasks], initial_completed)
        self.assertEqual(
            [self.browser.task_completed(task.target) for task in tasks], initial_completed
        )
        self.assertEqual(len({task.identity for task in tasks}), len(initial_completed))
        if layout == 'mixed':
            readers = [
                frame for frame in self.browser.page.frames
                if frame.locator('#same-reader').count()
            ]
            self.assertEqual(len(readers), 2)
            self.assertEqual(len({frame.url for frame in readers}), 1)
            self.assertEqual(
                len({frame.locator('#same-reader').inner_html() for frame in readers}), 1
            )
            self.assertGreater(
                self.browser.page.locator('#ext-gen1051').count(), 4,
                '真实图标id重复，不能按id全页关联状态',
            )
        solver = Mock()
        solver.solve.side_effect = AssertionError('本地任务状态回归不包含模型或答题')
        original = self.browser.process_content
        observed_counts = []
        callbacks = []

        def observe(*, on_quiz=None, on_task=None):
            original_key = self.browser.chapter_key()

            def task_started(kind, ordinal, total_known):
                callbacks.append((kind, ordinal))
                if on_task:
                    on_task(kind, ordinal, total_known)

            count = original(on_quiz=on_quiz, on_task=task_started)
            self.assertEqual(
                self.browser.chapter_key(), original_key,
                '网站完成标记变绿不能更换页面身份',
            )
            observed_counts.append(count)
            return count

        with patch.object(self.browser, 'process_content', side_effect=observe):
            pages = Workflow(
                self.browser, solver, self.settings, self.stop, self.logs.append
            ).run()
        solver.solve.assert_not_called()
        self.assertEqual(pages, 1)
        self.assertEqual(
            observed_counts, [len(executed_ids)],
            '返回值只统计实际执行，跳过已完成任务不计',
        )
        self.assertEqual(
            callbacks, [(kind, index + 1) for index, kind in enumerate(executed_kinds)]
        )
        self.assertEqual(self.browser.current_url, f'{self.base_url}/complete.html')
        metrics = self.metrics()
        self.assertEqual(metrics['nativeDone'], executed_ids)
        self.assertEqual(metrics['violations'], [])
        self.assertEqual(metrics['nextClicks'], 1)
        self.assertEqual(metrics['nextTrusted'], [True])
        self.assertEqual(
            metrics['events'][-1], {'id': 'next', 'event': 'click', 'trusted': True}
        )
        for task_id, task in metrics['tasks'].items():
            if task_id not in executed_ids:
                self.assertEqual(task['wheels'], 0)
                self.assertEqual(task['playClicks'], 0)
                continue
            if task['kind'] == 'document':
                self.assertGreater(task['wheels'], 0)
                self.assertTrue(task['wheelTrusted'])
                self.assertTrue(task['bottom'])
            else:
                self.assertEqual(task['playClicks'], 1)
                self.assertTrue(task['ended'])
                self.assertEqual(task['seekEvents'], 0)
                self.assertEqual(task['rate'], 1)
                self.assertAlmostEqual(task['time'], task['duration'], places=2)
                self.assertGreaterEqual(task['elapsed'] + .05, task['duration'])
        self.assertTrue(all(
            event['trusted'] for event in metrics['events'] if event['event'] == 'play-click'
        ))
        return metrics

    def test_completed_regions_skip_without_polluting_unfinished_siblings_and_identical_readers(self):
        self.run_tasks(
            'mixed', [True, False, True, False],
            ['video-2', 'document-4'], ['video', 'document'],
        )

    def test_all_completed_readers_players_and_unloaded_shells_skip_to_one_next(self):
        self.run_tasks('all-complete', [True, True, True, True], [], [])

    def test_completed_static_reader_is_skipped_before_unfinished_identical_reader(self):
        self.run_tasks('document-pair', [True, False], ['document-2'], ['document'])

    def test_css_only_completed_pdf_card_is_skipped_through_nested_iframes(self):
        self.run_tasks('static-css-pair', [True, False], ['document-2'], ['document'])

    def test_direct_read_document_skips_css_only_completed_pdf_card(self):
        self.launch_tasks('static-css-pair')
        icons = self.browser.page.locator('.ans-attach-ct > .ans-job-icon:not([hidden])')
        self.assertEqual(icons.count(), 2)
        for index in range(icons.count()):
            icon = icons.nth(index)
            self.assertIsNone(icon.get_attribute('aria-label'))
            self.assertIsNone(icon.get_attribute('title'))
            self.assertEqual(icon.inner_text(), '')
            self.assertEqual(icon.locator('*').count(), 0)
            self.assertEqual(icon.evaluate("e => getComputedStyle(e,'::before').content"), 'none')
        first_background = icons.nth(0).evaluate('e => getComputedStyle(e).backgroundImage')
        second_background = icons.nth(1).evaluate('e => getComputedStyle(e).backgroundImage')
        self.assertNotEqual(first_background, second_background)
        readers = [frame for frame in self.browser.page.frames if frame.locator('#same-reader').count()]
        self.assertEqual(len(readers), 2)
        self.assertTrue(all(frame.parent_frame.parent_frame is self.browser.page.main_frame for frame in readers))

        self.browser.read_document()

        metrics = self.metrics()
        self.assertEqual(metrics['nativeDone'], ['document-2'])
        self.assertEqual(metrics['tasks']['document-1']['wheels'], 0)
        self.assertEqual(metrics['tasks']['document-1']['scrolls'], 0)
        self.assertGreater(metrics['tasks']['document-2']['wheels'], 0)
        self.assertTrue(metrics['tasks']['document-2']['wheelTrusted'])
        self.assertTrue(metrics['tasks']['document-2']['bottom'])
        self.assertEqual(metrics['violations'], [])
        self.assertEqual(metrics['nextClicks'], 0)

    def test_visible_unfinished_label_overrides_finished_card_class(self):
        self.run_tasks('static-css-conflict', [False], ['document-1'], ['document'])

    def test_finished_card_with_only_hidden_markers_does_not_skip_reader(self):
        self.run_tasks('static-css-hidden', [False], ['document-1'], ['document'])

    def test_direct_read_document_does_not_scroll_completed_readers(self):
        self.launch_tasks('all-complete')

        self.browser.read_document()

        metrics = self.metrics()
        self.assertEqual(metrics['nativeDone'], [])
        self.assertEqual(metrics['nextClicks'], 0)
        self.assertTrue(all(task['wheels'] == 0 for task in metrics['tasks'].values()))
        self.assertTrue(all(task['scrolls'] == 0 for task in metrics['tasks'].values()))
        self.assertTrue(all(task['playClicks'] == 0 for task in metrics['tasks'].values()))
        self.assertEqual(self.browser.page.evaluate('document.scrollingElement.scrollTop'), 0)

    def test_direct_read_document_scrolls_only_unfinished_identical_reader(self):
        self.launch_tasks('document-pair')

        self.browser.read_document()

        metrics = self.metrics()
        self.assertEqual(metrics['nativeDone'], ['document-2'])
        self.assertEqual(metrics['tasks']['document-1']['wheels'], 0)
        self.assertEqual(metrics['tasks']['document-1']['scrolls'], 0)
        unfinished = metrics['tasks']['document-2']
        self.assertGreater(unfinished['wheels'], 0)
        self.assertTrue(unfinished['wheelTrusted'])
        self.assertTrue(unfinished['bottom'])
        self.assertTrue(all(task['playClicks'] == 0 for task in metrics['tasks'].values()))
        self.assertEqual(metrics['violations'], [])
        self.assertEqual(metrics['nextClicks'], 0)

    def assert_document_stops_after_first_wheel(self, layout):
        from gaybar_tools.workflow import Workflow

        self.launch_tasks(layout)
        solver = Mock()
        solver.solve.side_effect = AssertionError('完成状态回归不包含模型或答题')

        pages = Workflow(
            self.browser, solver, self.settings, self.stop, self.logs.append
        ).run()

        solver.solve.assert_not_called()
        self.assertEqual(pages, 1)
        self.assertEqual(self.browser.current_url, f'{self.base_url}/complete.html')
        metrics = self.metrics()
        task = metrics['tasks']['document-1']
        self.assertEqual(task['wheels'], 1, '标记完成后不能继续发送滚轮')
        self.assertTrue(task['wheelTrusted'])
        self.assertTrue(task['markedComplete'])
        self.assertGreater(task['top'], 0)
        self.assertFalse(task['bottom'], '无需为已完成任务继续滚到底')
        self.assertEqual(metrics['nativeDone'], [])
        self.assertEqual(metrics['violations'], [])
        self.assertEqual(metrics['nextClicks'], 1)
        self.assertEqual(metrics['nextTrusted'], [True])

    def test_document_marked_complete_after_first_wheel_stops_before_bottom(self):
        self.assert_document_stops_after_first_wheel('complete-after-wheel')

    def test_css_only_document_marked_complete_after_first_wheel_stops_before_bottom(self):
        self.assert_document_stops_after_first_wheel('static-css-after-wheel')

    def test_direct_read_document_stops_when_first_wheel_marks_task_complete(self):
        self.launch_tasks('complete-after-wheel')

        self.browser.read_document()

        metrics = self.metrics()
        task = metrics['tasks']['document-1']
        self.assertEqual(task['wheels'], 1)
        self.assertTrue(task['wheelTrusted'])
        self.assertTrue(task['markedComplete'])
        self.assertGreater(task['top'], 0)
        self.assertFalse(task['bottom'])
        self.assertEqual(metrics['nativeDone'], [])
        self.assertEqual(metrics['nextClicks'], 0)

    def test_task_marked_complete_before_its_turn_is_skipped(self):
        metrics = self.run_tasks(
            'before-turn', [False, False, False],
            ['document-1', 'document-3'], ['document', 'document'],
        )
        self.assertTrue(metrics['tasks']['video-2']['markedComplete'])
        self.assertTrue(any(
            event.get('reason') == 'website-update-before-turn' for event in metrics['events']
        ))

    def test_video_turning_green_at_ninety_percent_still_ends_naturally(self):
        metrics = self.run_tasks('progress-green', [False], ['video-1'], ['video'])
        marker = next(
            event for event in metrics['events'] if event.get('reason') == 'ninety-percent'
        )
        ended = next(event for event in metrics['events'] if event['event'] == 'ended')
        self.assertGreaterEqual(marker['time'], marker['duration'] * .9)
        self.assertLess(marker['time'], ended['time'])
        self.assertLess(metrics['events'].index(marker), metrics['events'].index(ended))

    def test_completed_empty_region_does_not_skip_adjacent_unfinished_empty_attachment(self):
        from gaybar_tools.browser import BrowserError
        from gaybar_tools.workflow import Workflow

        self.launch_tasks('unfinished-empty')
        self.assertEqual(
            [task.completed for task in self.browser.content_tasks()], [True, False]
        )
        solver = Mock()
        with patch.object(self.browser, 'NEXT_PAGE_TIMEOUT', 1), patch.object(
            self.browser, 'next_chapter', wraps=self.browser.next_chapter
        ) as next_page:
            with self.assertRaisesRegex(BrowserError, '附件.*未能读取|附件.*加载'):
                Workflow(
                    self.browser, solver, self.settings, self.stop, self.logs.append
                ).run()
        next_page.assert_not_called()
        solver.solve.assert_not_called()
        self.assertEqual(self.metrics()['nextClicks'], 0)
        self.assertEqual(self.metrics()['nativeDone'], [])

    def test_completed_placeholder_mounting_reader_after_next_is_not_new_material(self):
        from gaybar_tools.browser import BrowserError
        from gaybar_tools.workflow import Workflow

        self.launch_tasks('late-complete-mount')
        original_key, original_url = self.browser.chapter_key(), self.browser.current_url
        solver = Mock()
        with patch.object(self.browser, 'NEXT_PAGE_TIMEOUT', 2):
            with self.assertRaises(BrowserError):
                Workflow(
                    self.browser, solver, self.settings, self.stop, self.logs.append
                ).run()
        solver.solve.assert_not_called()
        self.browser.page.wait_for_function("window.completionFixtureReady()")
        self.assertEqual(self.browser.current_url, original_url)
        self.assertEqual(self.browser.chapter_key(), original_key)
        metrics = self.metrics()
        self.assertTrue(metrics['mountedAfterNext'])
        self.assertTrue(metrics['tasks']['document-1']['ready'])
        self.assertEqual(metrics['tasks']['document-1']['wheels'], 0)
        self.assertEqual(metrics['nextClicks'], 1)
        self.assertEqual(metrics['nextTrusted'], [True])

    def test_completed_video_same_jobid_and_dom_with_new_source_is_new_material(self):
        self.launch_tasks('changed-complete-video')
        original_key, original_url = self.browser.chapter_key(), self.browser.current_url
        card = self.browser.page.locator('.ans-attach-ct').element_handle()
        iframe = self.browser.page.locator('.ans-attach-ct iframe').element_handle()
        frame = iframe.content_frame()
        video = frame.locator('video').element_handle()
        for handle in (card, iframe, video):
            self.addCleanup(handle.dispose)
        original_source, original_frame_url = video.get_attribute('src'), frame.url
        self.assertEqual(iframe.get_attribute('jobid'), 'fixed-job')
        self.assertEqual([task.completed for task in self.browser.content_tasks()], [True])
        self.assertEqual(self.browser.process_content(), 0)

        self.assertTrue(self.browser.next_chapter())

        self.assertEqual(self.browser.current_url, original_url)
        self.assertNotEqual(self.browser.chapter_key(), original_key)
        self.assertEqual(frame.url, original_frame_url)
        self.assertIs(iframe.content_frame(), frame)
        self.assertTrue(card.evaluate("e => e === document.querySelector('.ans-attach-ct')"))
        self.assertTrue(iframe.evaluate("e => e === document.querySelector('.ans-attach-ct iframe')"))
        self.assertTrue(video.evaluate("e => e === document.querySelector('video')"))
        self.assertEqual(iframe.get_attribute('jobid'), 'fixed-job')
        self.assertEqual(video.get_attribute('src'), original_source + '?material=2')
        self.assertEqual(self.browser.chapter_kind(), 'video')
        self.assertEqual([task.completed for task in self.browser.content_tasks()], [True])
        self.assertTrue(video.evaluate('e => e.paused && e.currentTime === 0'))
        metrics = self.metrics()
        self.assertTrue(metrics['materialChangedAfterNext'])
        self.assertEqual(metrics['tasks']['video-1']['playClicks'], 0)
        self.assertEqual(metrics['tasks']['video-1']['wheels'], 0)
        self.assertEqual(metrics['nativeDone'], [])
        self.assertEqual(metrics['nextClicks'], 1)
        self.assertEqual(metrics['nextTrusted'], [True])

    def test_completed_empty_iframe_with_new_objectid_is_ready_new_material(self):
        self.launch_tasks('changed-complete-placeholder')
        original_key, original_url = self.browser.chapter_key(), self.browser.current_url
        card = self.browser.page.locator('.ans-attach-ct').element_handle()
        iframe = self.browser.page.locator('.ans-attach-ct iframe').element_handle()
        frame = iframe.content_frame()
        for handle in (card, iframe):
            self.addCleanup(handle.dispose)
        self.assertEqual(frame.url, 'about:blank')
        self.assertEqual(iframe.get_attribute('objectid'), 'resource-A')
        tasks = self.browser.content_tasks()
        self.assertEqual([(task.completed, task.placeholder) for task in tasks], [(True, True)])
        self.assertEqual(self.browser.process_content(), 0)

        self.assertTrue(self.browser.next_chapter())

        self.assertEqual(self.browser.current_url, original_url)
        self.assertNotEqual(self.browser.chapter_key(), original_key)
        self.assertEqual(frame.url, 'about:blank')
        self.assertEqual(iframe.get_attribute('src'), 'about:blank')
        self.assertIs(iframe.content_frame(), frame)
        self.assertTrue(card.evaluate("e => e === document.querySelector('.ans-attach-ct')"))
        self.assertTrue(iframe.evaluate("e => e === document.querySelector('.ans-attach-ct iframe')"))
        self.assertEqual(iframe.get_attribute('objectid'), 'resource-B')
        self.assertEqual(self.browser.chapter_kind(), 'document')
        tasks = self.browser.content_tasks()
        self.assertEqual([(task.completed, task.placeholder) for task in tasks], [(True, True)])
        metrics = self.metrics()
        self.assertTrue(metrics['materialChangedAfterNext'])
        self.assertEqual(metrics['tasks']['document-1']['wheels'], 0)
        self.assertEqual(metrics['tasks']['document-1']['playClicks'], 0)
        self.assertEqual(metrics['nativeDone'], [])
        self.assertEqual(metrics['nextClicks'], 1)
        self.assertEqual(metrics['nextTrusted'], [True])

    def test_iframe_data_json_progress_does_not_change_material_identity(self):
        self.launch_tasks('unfinished-empty')
        iframe = self.browser.page.locator('.ans-attach-ct iframe').first
        iframe.evaluate("""(e, data) => {
            e.removeAttribute('src');
            e.setAttribute('data', JSON.stringify(data));
        }""", {'objectid': 'same-resource', 'isPassed': True, 'progress': 99})
        self.assertIsNone(iframe.get_attribute('src'))
        original_key = self.browser.chapter_key()
        self.assertTrue(self.browser.task_completed(iframe))

        iframe.evaluate("""e => {
            const data = JSON.parse(e.getAttribute('data'));
            data.progress = 100;
            e.setAttribute('data', JSON.stringify(data));
        }""")

        self.assertEqual(self.browser.chapter_key(), original_key)
        self.assertTrue(self.browser.task_completed(iframe))
        self.assertEqual(
            iframe.evaluate("e => JSON.parse(e.getAttribute('data'))"),
            {'objectid': 'same-resource', 'isPassed': True, 'progress': 100},
        )
        metrics = self.metrics()
        self.assertTrue(all(task['wheels'] == 0 for task in metrics['tasks'].values()))
        self.assertTrue(all(task['playClicks'] == 0 for task in metrics['tasks'].values()))
        self.assertEqual(metrics['nextClicks'], 0)


if __name__ == '__main__':
    unittest.main()
