"""Execute model BigQuery SQL against synthetic DuckDB data, not an Omni compiler.

Run: python -m unittest discover -s tests -v
"""
import json
from pathlib import Path
import re
import unittest

import duckdb
import sqlglot
from sqlglot import exp
import yaml

MODEL = Path(__file__).resolve().parents[1] / 'omni/TelligentSourcegraph'
EVENTS = yaml.safe_load((MODEL / 'telligentsourcegraph.telemetry/v2_events.view.yaml').read_text())
STARTS = yaml.safe_load((MODEL / 'abc_mcp_starts.query.view.yaml').read_text())
DAY = '2026-11-01'


def expand(model, name):
    field = model['dimensions'].get(name, model.get('measures', {}).get(name))
    expression = field.get('sql', name)
    return re.sub(r'\$\{(\w+)\}', lambda m: '(' + expand(model, m[1]) + ')', expression)


def duck_sql(sql):
    tree = sqlglot.parse_one(sql, read='bigquery')
    # Test tables retain the production schema/table, in an in-memory catalog.
    for table in tree.find_all(exp.Table):
        table.set('catalog', None)
    return tree.sql(dialect='duckdb', unsupported_level=sqlglot.ErrorLevel.RAISE)


class ABCMetricsTest(unittest.TestCase):
    def setUp(self):
        self.db = duckdb.connect()
        self.db.execute('CREATE SCHEMA telemetry; CREATE SCHEMA sourcegraph_analytics')
        self.db.execute('''
          CREATE TABLE telemetry.v2_events (
            id VARCHAR, timestamp TIMESTAMPTZ, publish_time TIMESTAMPTZ,
            externalUrl VARCHAR, uniqueUserId VARCHAR, eventName VARCHAR,
            parameters STRUCT(metadata JSON, version INTEGER),
            server STRUCT(apiRequestClient STRUCT(userAgent VARCHAR)),
            userMetadata STRUCT(serviceAccount BOOLEAN)
          )
        ''')
        self.db.execute('''CREATE TABLE sourcegraph_analytics.abc_funnel (
          agent_key VARCHAR, step_order INTEGER, reached_at TIMESTAMPTZ)''')
        self.db.execute('''CREATE TABLE sourcegraph_analytics.server_instances (
          serverEndpoint VARCHAR, sfdc_unique_id VARCHAR, sfdc_name VARCHAR, instance_type VARCHAR)''')

    def tearDown(self):
        self.db.close()

    def event(self, uuid, tool, agent=None, instance='alpha.test', outcome=1,
              endpoint=8, client='client-a', user='1', duration=100, day=DAY, version=6):
        metadata = {'endpoint': endpoint, 'toolCallId': tool, 'terminalState': outcome}
        if agent is not None:
            metadata['agentID'] = agent
        if duration is not None:
            metadata['durationMs'] = duration
        self.db.execute('INSERT INTO telemetry.v2_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', [
            uuid, day + ' 12:00:00+00', day + ' 12:01:00+00', instance,
            instance + '|' + user, 'search:mcp.toolcall',
            {'metadata': json.dumps(metadata), 'version': version},
            {'apiRequestClient': {'userAgent': client}},
            {'serviceAccount': user == 'service'},
        ])

    def fixture(self):
        self.event('e1', 26, 101)
        self.event('e1', 26, 101)  # Duplicate export, not a second call.
        self.event('e2', 26, 101, client='later-client', day='2026-11-02')
        self.event('e3', 34, 102, user='service')
        self.event('e4', 26, 101, instance='beta.test')
        self.event('e5', 26, 103)
        self.event('e6', 26, outcome=2)
        self.event('e7', 26, version=5)
        self.event('e8', 27)  # Reads MCP-created agent 101.
        self.event('e9', 27)  # Reads UI-created agent 900.
        self.event('e10', 19)
        self.event('e11', 18, endpoint=7)
        for key, last in [('alpha.test|101', 5), ('alpha.test|102', 4),
                          ('beta.test|101', 3), ('alpha.test|900', 5)]:
            for step in range(1, last + 1):
                self.db.execute('INSERT INTO sourcegraph_analytics.abc_funnel VALUES (?, ?, ?)',
                                [key, step, '2026-11-03 12:00:00+00'])

    def dimensions(self, names):
        expressions = ', '.join(expand(EVENTS, name) + ' AS ' + name for name in names)
        return self.db.execute(duck_sql('SELECT ' + expressions + ' FROM telemetry.v2_events ORDER BY id')).fetchall()

    def measure(self, name, where='TRUE'):
        field = EVENTS['measures'][name]
        value = duck_sql(expand(EVENTS, name))
        kind = field['aggregate_type']
        if kind == 'percentile_distinct_on':
            self.assertEqual(field['custom_primary_key_sql'], '${id}')
            sql = f'''SELECT quantile_cont(value, {field['percentile'] / 100}) FROM
              (SELECT id, ANY_VALUE({value}) AS value FROM telemetry.v2_events WHERE {where} GROUP BY id)'''
        else:
            aggregate = 'COUNT(DISTINCT ' if kind == 'count_distinct' else 'MAX('
            sql = f'SELECT {aggregate}{value}) FROM telemetry.v2_events WHERE {where}'
        return self.db.execute(sql).fetchone()[0]

    def cohort(self):
        return self.db.execute(duck_sql(STARTS['sql']) + ' ORDER BY agent_key').fetchall()

    def conversion(self, where='TRUE'):
        runs = f"COUNT(DISTINCT {expand(STARTS, 'runs')})"
        merged = f"COUNT(DISTINCT {expand(STARTS, 'runs_merged')})"
        conversion = STARTS['measures']['start_to_merge_conversion']['sql'].replace('${runs}', runs).replace('${runs_merged}', merged)
        sql = f"SELECT {runs}, {merged}, {conversion} FROM ({STARTS['sql']}) WHERE {where}"
        return self.db.execute(duck_sql(sql)).fetchone()

    def test_exact_acceptance_fixture(self):
        self.fixture()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM telemetry.v2_events').fetchone()[0], 12)
        for measure, expected in {
            'abc_mcp_calls': 9, 'abc_mcp_discovery_calls': 1,
            'abc_mcp_start_calls': 7, 'abc_mcp_attributed_start_calls': 5,
            'abc_mcp_unknown_start_calls': 1, 'abc_mcp_users': 3, 'abc_mcp_instances': 2,
        }.items():
            with self.subTest(measure=measure):
                self.assertEqual(self.measure(measure), expected)
        for mode, expected in [('Attended', 6), ('Unattended', 1)]:
            self.assertEqual(self.measure('abc_mcp_start_calls', duck_sql(expand(EVENTS, 'abc_mcp_start_mode')) + f" = '{mode}'"), expected)
        for outcome, expected in [('Success', 6), ('Failure', 1)]:
            self.assertEqual(self.measure('abc_mcp_start_calls', duck_sql(expand(EVENTS, 'mcp_terminal_state')) + f" = '{outcome}'"), expected)
        self.assertEqual(self.conversion(), (4, 1, 0.25))
        self.assertEqual([r[0] for r in self.cohort()], ['alpha.test|101', 'alpha.test|102', 'alpha.test|103', 'beta.test|101'])
        self.assertEqual(self.cohort()[0][5], 'client-a')
        self.assertIsNone(self.cohort()[2][10])  # Unmatched Start remains in denominator.
        self.assertIsNone(self.cohort()[3][10])  # No cross-instance merge leakage.
        # Repeated merge-stage observations cannot add another converted run.
        self.db.execute("INSERT INTO sourcegraph_analytics.abc_funnel VALUES ('alpha.test|101', 5, '2026-11-04 12:00:00+00')")
        self.assertEqual(self.conversion(), (4, 1, 0.25))

    def test_replays_duplicates_out_of_order_and_cohort_date(self):
        self.fixture()
        before = self.cohort()
        self.db.execute('INSERT INTO telemetry.v2_events SELECT * FROM telemetry.v2_events ORDER BY id DESC')
        self.db.execute('INSERT INTO sourcegraph_analytics.abc_funnel SELECT * FROM sourcegraph_analytics.abc_funnel')
        self.assertEqual(self.cohort(), before)
        self.assertEqual(self.measure('abc_mcp_calls'), 9)
        self.assertEqual(self.conversion(), (4, 1, 0.25))
        self.assertEqual(self.conversion("DATE(observed_start_at) = DATE '2026-11-01'"), (4, 1, 0.25))
        self.assertEqual(self.conversion("DATE(observed_start_at) = DATE '2026-11-02'"), (0, 0, None))
        self.event('earlier', 26, 101, client='earliest-client', day='2026-10-31')
        self.assertEqual(self.cohort()[0][5], 'earliest-client')
        self.assertEqual(self.conversion("DATE(observed_start_at) = DATE '2026-11-01'"), (3, 0, 0.0))

    def test_labels_unknowns_and_terminal_states(self):
        labels = {
            24: 'Read Agent File', 25: 'Chart', 26: 'Start batch change', 27: 'Read batch change',
            28: 'Decide batch change approval', 29: 'Message batch change', 30: 'List batch change workspaces',
            31: 'Stop batch change', 33: 'List batch changes', 34: 'Start batch change unattended',
            35: 'Answer batch change question', 36: 'Read batch change workspace diff',
            37: 'Cancel batch change turn', 38: 'List batch change standing approvals',
            39: 'Revoke batch change standing approval', 40: 'Read batch change spec',
            41: 'Read batch change transcript', 42: 'Read batch change workspace logs',
            32: '32', 999: '999', None: 'Unknown',
        }
        for tool, label in labels.items():
            with self.subTest(tool=tool):
                self.db.execute('DELETE FROM telemetry.v2_events')
                self.event('mapping', tool)
                self.assertEqual(self.dimensions(['mcp_tool_call_category']), [(label,)])
                self.assertEqual(self.measure('abc_mcp_calls'), 1)
        for endpoint, label in [(1, 'MCP'), (2, 'MCPV1'), (3, 'DeepSearch'), (4, 'DeepSearchPlumbing'),
                                (5, '5'), (6, 'MCP all'), (7, 'Batches executor'), (8, 'ABC'), (999, '999'), (None, 'Unknown')]:
            with self.subTest(endpoint=endpoint):
                self.db.execute('DELETE FROM telemetry.v2_events')
                self.event('mapping', 26, endpoint=endpoint)
                self.assertEqual(self.dimensions(['mcp_endpoint']), [(label,)])
                self.assertEqual(self.measure('abc_mcp_calls'), int(endpoint == 8))
        for state, label in [(1, 'Success'), (2, 'Failure'), (3, 'Timeout'), (4, 'Canceled'), (5, 'Invalid request'), (999, '999'), (None, 'Unknown')]:
            with self.subTest(state=state):
                self.db.execute('DELETE FROM telemetry.v2_events')
                self.event('mapping', 26, outcome=state)
                self.assertEqual(self.dimensions(['mcp_terminal_state']), [(label,)])

    def test_missing_or_invalid_attribution_is_not_a_run(self):
        for i, agent in enumerate([None, 0, -1, 'malformed']):
            self.event(str(i), 26, agent, version=5)
        self.event('missing-instance', 34, 101, instance='')
        self.event('failed-with-id', 26, 101, outcome=2)
        self.event('read-with-id', 27, 900)
        self.assertEqual(self.measure('abc_mcp_unknown_start_calls'), 5)
        self.assertEqual(self.conversion(), (0, 0, None))

    def test_latency_deduplicates_uuid_not_value_and_users_span_days(self):
        for i, duration in enumerate([10, 20, 20, 40, None]):
            self.event(f'latency-{i}', 27, duration=duration)
        self.event('latency-0', 27, duration=10)
        self.event('discovery', 19, duration=10000)
        self.event('other-endpoint', 18, endpoint=7, duration=10000)
        self.assertEqual(self.measure('abc_mcp_p50_duration'), 20)
        self.assertAlmostEqual(self.measure('abc_mcp_p95_duration'), 37)
        self.event('next-day', 31, day='2026-11-02')
        self.assertEqual(self.measure('abc_mcp_users'), 1)
        self.assertEqual(self.measure('abc_mcp_instances'), 1)

    def test_account_lookup_deduplicates_endpoints_and_retains_unmatched_starts(self):
        self.fixture()
        self.db.execute('''INSERT INTO sourcegraph_analytics.server_instances VALUES
          ('alpha.test', 'secondary-account', 'Secondary account', 'Secondary'),
          ('ALPHA.TEST', 'primary-account', 'Primary account', 'Primary'),
          ('alpha.test', NULL, NULL, 'Primary')''')
        self.assertEqual(self.conversion(), (4, 1, 0.25))
        self.assertEqual(self.conversion("customer_key = 'primary-account'"), (3, 1, 1/3))
        self.assertEqual(self.cohort()[2][-1], 'Primary account')
        self.assertEqual(self.cohort()[3][-1], 'beta.test')


if __name__ == '__main__':
    unittest.main()
