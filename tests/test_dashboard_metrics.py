import json
import unittest
from unittest.mock import Mock, patch

from webapp.app import build_job_progress, parse_live_progress, summarize_job_logs
from webapp.config import WebAppConfig, EcsBackendConfig
from webapp.ecs_jobs import read_ecs_task_logs
from webapp.jobs import persist_ecs_result_artifacts


class DashboardMetricTests(unittest.TestCase):
    def summary(self, dry_run=False):
        return {"product_count":2143, "discovered_product_url_count":2143,
                "dry_run":dry_run,"validation":{"performed":True,"errors":[]},
                "sync_summary":{"created_products":2132,"updated_products":11,
                                "failed_products":0,"errors":[]},
                "stages":{"sync":{"performed":True,"uploaded_products":2143}}}

    def progress(self, summary):
        line = 'INFO RESULT_RUN_SUMMARY ' + json.dumps(summary)
        job = {"backend":"ecs_fargate","remote_job_id":"task80","status":"succeeded"}
        with patch('webapp.app.load_job_logs',return_value=('group','stream',line)):
            return build_job_progress(job, WebAppConfig())

    def test_completed_run_uses_task_summary_and_not_false_error_keywords(self):
        result = self.progress(self.summary())
        self.assertEqual(result['uploaded_count'],2143)
        self.assertEqual(result['created_count'],2132)
        self.assertEqual(result['updated_count'],11)
        self.assertEqual(result['scraped_count'],2143)
        self.assertEqual(result['errors'],[])
        self.assertNotIn('Potential errors', summarize_job_logs('INFO RESULT_RUN_SUMMARY '+json.dumps(self.summary())))

    def test_dry_run_never_claims_actual_uploads(self):
        self.assertEqual(self.progress(self.summary(True))['uploaded_count'],0)

    def test_real_structured_errors_are_still_visible(self):
        summary=self.summary()
        summary['sync_summary']['errors']=['product_id=bad: invalid payload']
        self.assertIn('product_id=bad: invalid payload', self.progress(summary)['errors'])

    def test_upload_progress_is_parsed_while_sync_is_running(self):
        progress = parse_live_progress('INFO PROGRESS phase=syncing uploaded=123 created=120 updated=3 failed=2 total=2143')
        self.assertEqual(progress['uploaded_count'],123)
        self.assertEqual(progress['updated_count'],3)
        self.assertEqual(progress['failed_count'],2)
        self.assertEqual(progress['phase'],'syncing')

    def test_cloudwatch_follows_pages_and_includes_final_result(self):
        client=Mock()
        marker='INFO RESULT_RUN_SUMMARY '+json.dumps(self.summary())
        client.get_log_events.side_effect=[
            {'events':[{'message':f'line{i}'} for i in range(600)],'nextForwardToken':'page2'},
            {'events':[{'message':marker}],'nextForwardToken':'end'},
            {'events':[],'nextForwardToken':'end'},
        ]
        with patch('webapp.ecs_jobs._logs_client',return_value=client):
            _,_,logs=read_ecs_task_logs(EcsBackendConfig(),task_arn='task80',log_group='group',log_stream='stream',limit=500)
        self.assertEqual(len(logs.splitlines()),500)
        self.assertIn(marker,logs)
        self.assertEqual(client.get_log_events.call_count,3)
        self.assertEqual(client.get_log_events.call_args_list[1].kwargs['nextToken'],'page2')

    def test_overview_recovers_completed_task_and_caches_its_own_result(self):
        import tempfile
        from pathlib import Path
        from models import SupplierConfig, SupplierRunState
        from webapp.service import supplier_health_summary
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            paths={'output_dir':root,'run_summary':root/'missing_summary.json',
                   'sync_report':root/'missing_sync.json','state_file':root/'state.json'}
            supplier=SupplierConfig('merat',False,'merat','https://shop','TOKEN','output/merat')
            conn=Mock()
            conn.execute.return_value.fetchone.return_value=None
            job={'id':80,'backend':'ecs_fargate','remote_job_id':'task80','status':'succeeded'}
            with patch('webapp.service.load_supplier_configs',return_value=[supplier]), patch('webapp.service.supplier_paths',return_value=paths), patch('webapp.service.load_state',return_value=SupplierRunState('merat')), patch('webapp.service.list_jobs',return_value=[job]), patch('webapp.service.supplier_structure_status',return_value={}), patch('webapp.service.onboarding_status',return_value={}), patch('webapp.jobs.read_ecs_task_logs',return_value=('group','stream','INFO RESULT_RUN_SUMMARY '+json.dumps(self.summary()))) as logs:
                first=supplier_health_summary(conn,WebAppConfig())[0]
                second=supplier_health_summary(conn,WebAppConfig())[0]
            self.assertEqual(first['last_sync_summary']['created_products'],2132)
            self.assertEqual(second['product_count'],2143)
            self.assertEqual(logs.call_count,1)
            self.assertTrue((root/'job_80_run_summary.json').exists())
