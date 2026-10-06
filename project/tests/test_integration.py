"""Сквозные тесты настоящих HTTP-сервисов. Базы всегда временные."""
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.port = 18360
        for candidate in range(18360, 19360, 4):
            probes = []
            try:
                for i in range(4):
                    probe = socket.socket()
                    probes.append(probe)
                    probe.bind(("127.0.0.1", candidate + i))
                cls.port = candidate
                break
            except OSError:
                pass
            finally:
                for p in probes:
                    p.close()
        cls.env = dict(os.environ, DEMO_DATA_DIR=cls.temp.name, DEMO_PORT=str(cls.port))
        cls.processes = {}
        cls.client = httpx.Client(base_url=f"http://127.0.0.1:{cls.port}", trust_env=False, timeout=15)
        for name, offset in (("erp",1),("wms",2),("tms",3),("hub",0)):
            cls.start_service(name, offset)

    @classmethod
    def start_service(cls, name, offset):
        cls.processes[name] = subprocess.Popen([sys.executable,"-m","uvicorn",f"services.{name}:app",
            "--host","127.0.0.1","--port",str(cls.port+offset),"--log-level","error"],cwd=ROOT,env=cls.env)
        for _ in range(100):
            try:
                if cls.client.get(f"http://127.0.0.1:{cls.port+offset}/health").status_code==200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        raise RuntimeError(f"{name} did not start")

    @classmethod
    def tearDownClass(cls):
        for p in cls.processes.values():
            p.terminate()
        for p in cls.processes.values():
            p.wait(timeout=10)
        cls.client.close()
        cls.temp.cleanup()

    def setUp(self):
        self.client.post('/api/clock',json={'running':False,'speed':60}).raise_for_status()
        for name in ('erp','wms','tms'):
            self.fault(name)

    def fault(self,name,**kwargs):
        self.client.post('/api/fault/'+name,json=dict(delay_ms=0,fail_next=0,lost_next=0,**{})|kwargs).raise_for_status()

    def state(self):
        return self.client.get('/api/state').json()

    def create(self,**kwargs):
        now=self.state()['clock']
        # Разнесённые интервалы исключают конкуренцию тестов за машины.
        ahead = 10800 + (len(self.state()['jobs']) * 7200)
        data=dict(id=str(uuid.uuid4()),customer='Тестовый получатель',sku='BOX-01',quantity=3,
                  window_start=now+ahead,window_end=now+ahead+900,picking_min=1,loading_min=1,travel_min=1,buffer_min=0)
        data.update(kwargs)
        self.client.post('/api/orders',json=data).raise_for_status()
        return data

    def wait_job(self,oid,predicate,timeout=25):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            j=next((j for j in self.state()['jobs'] if j['order']['id']==oid),None)
            if j and predicate(j):
                return j
            time.sleep(.15)
        self.fail(f'Timeout for {oid}: {j}')

    def command(self,service,body,key):
        offset={'erp':1,'wms':2,'tms':3}[service]
        return self.client.post(f'http://127.0.0.1:{self.port+offset}/commands',json=body,headers={'Idempotency-Key':key})

    def test_01_normal_flow_and_delivery(self):
        now=self.state()['clock']
        o=self.create(window_start=now+600,window_end=now+1800)
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        self.client.post('/api/clock',json={'running':True,'speed':600}).raise_for_status()
        j=self.wait_job(o['id'],lambda j:j['state']=='done')
        self.assertTrue(o['window_start']<=j['actual']['complete']<=o['window_end'])
        s=self.state()
        self.assertEqual(s['systems']['erp']['data']['orders'][o['id']]['status'],'delivered')
        self.assertEqual(s['systems']['wms']['data']['reservations'][o['id']]['status'],'consumed')
        self.assertEqual(s['systems']['tms']['data']['shipments'][o['id']]['status'],'arrived')

    def test_02_lost_confirmation_no_duplicate(self):
        self.fault('tms',lost_next=1)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        s=self.state()
        attempts=[l for l in s['logs'] if l['order_id']==o['id'] and l['stage']=='book']
        self.assertEqual(len(attempts),2)
        self.assertEqual(sum(key==o['id'] for key in s['systems']['tms']['data']['shipments']),1)
        self.assertEqual(attempts[0]['code'],200)
        self.assertEqual(attempts[1]['code'],504)

    def test_03_exhaustion_manual_retry(self):
        self.fault('tms',fail_next=4)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['state']=='dead')
        self.assertEqual(len([l for l in self.state()['logs'] if l['order_id']==o['id'] and l['stage']=='book']),4)
        self.client.post('/api/jobs/'+o['id']+'/retry',json={}).raise_for_status()
        self.wait_job(o['id'],lambda j:j['stage']=='ready')

    def test_04_compensation_and_business_error(self):
        before=self.state()['systems']['wms']['data']['products'][0]['stock']
        self.fault('tms',fail_next=4)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['state']=='retry')
        self.fault('tms')
        self.client.post('/api/jobs/'+o['id']+'/cancel',json={}).raise_for_status()
        self.wait_job(o['id'],lambda j:j['state']=='cancelled')
        self.assertEqual(self.state()['systems']['wms']['data']['products'][0]['stock'],before)
        o2=self.create(quantity=10000)
        self.wait_job(o2['id'],lambda j:j['state']=='dead')
        logs=[l for l in self.state()['logs'] if l['order_id']==o2['id']]
        self.assertEqual(len(logs),1)
        self.assertEqual(logs[0]['code'],409)

    def test_05_restart_resumes_persisted_job(self):
        self.fault('tms',fail_next=1)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['state']=='retry')
        self.processes['hub'].terminate()
        self.processes['hub'].wait(timeout=10)
        self.start_service('hub',0)
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        self.assertEqual(self.state()['systems']['wms']['data']['reservations'][o['id']]['quantity'],o['quantity'])

    def test_06_successful_sla_violation(self):
        self.fault('wms',delay_ms=2500)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        log=next(l for l in self.state()['logs'] if l['order_id']==o['id'] and l['stage']=='reserve')
        self.assertTrue(log['success'])
        self.assertFalse(log['sla_ok'])
        self.assertGreaterEqual(log['elapsed_ms'],2500)

    def test_07_idempotency_key_conflict(self):
        o=self.create()
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        altered=dict(o,quantity=o['quantity']+1)
        r=self.command('wms',{'action':'reserve','order_id':o['id'],'data':altered},o['id']+':reserve')
        self.assertEqual(r.status_code,409)

    def test_08_validation(self):
        now=self.state()['clock']
        r=self.client.post('/api/orders',json={'id':str(uuid.uuid4()),'customer':'X','sku':'BOX-01',
            'quantity':0,'window_start':now+100,'window_end':now})
        self.assertEqual(r.status_code,422)

    def test_09_timeout_after_commit_no_double_reserve(self):
        before=self.state()['systems']['wms']['data']['products'][0]['stock']
        self.fault('wms',delay_ms=6000)
        o=self.create()
        self.wait_job(o['id'],lambda j:j['state']=='retry')
        self.fault('wms')
        self.wait_job(o['id'],lambda j:j['stage']=='ready')
        s=self.state()
        self.assertEqual(s['systems']['wms']['data']['products'][0]['stock'],before-o['quantity'])
        logs=[l for l in s['logs'] if l['order_id']==o['id'] and l['stage']=='reserve']
        self.assertEqual([l['code'] for l in logs],[200,0])

    def test_10_erp_outbox_survives_hub_downtime(self):
        now=self.state()['clock']
        self.processes['hub'].terminate()
        self.processes['hub'].wait(timeout=10)
        o=dict(id=str(uuid.uuid4()),customer='Заказ при остановленном Hub',sku='PART-02',quantity=1,
               window_start=now+1000000,window_end=now+1000900)
        r=self.client.post(f'http://127.0.0.1:{self.port+1}/orders',json=o)
        r.raise_for_status()
        self.start_service('hub',0)
        self.wait_job(o['id'],lambda j:j['stage']=='ready')


if __name__=='__main__':
    unittest.main(verbosity=2)
