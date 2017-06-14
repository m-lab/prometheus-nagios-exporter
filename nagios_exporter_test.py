#!/usr/bin/python

import contextlib
import io
import json
import mock
import os
import socket
import textwrap
import unittest

import nagios_exporter


class FakeSocketIO(object):
    """An in-memory, socket-like object for unit tests."""

    def __init__(self, initial_value=''):
        self._writer = io.BytesIO()
        self._reader = io.BytesIO(initial_value)

    def shutdown(self, unused_path):
        pass

    def close(self):
        pass

    def recv(self, count=-1):
        """Reads count bytes from socket, or until EOF when count is -1."""
        return self._reader.read(count)

    def sendall(self, message):
        """Writes message to socket."""
        return self._writer.write(message)


def fixed16(code, length):
    """Creates a fixed16 header."""
    return "%s %11d\n" % (code, length)


class NagiosExporterTest(unittest.TestCase):

    def setUp(self):
        # A sample service record.
        self.services = [
            list(nagios_exporter.Service(
                host_name='localhost',
                service_description='Current Load',
                state=0,
                latency=0.078,
                process_performance_data=1,
                perf_data='load1=0.560;5.000;10.000;0;',
                check_command='check_load!5.0!4.0!3.0!10.0!6.0!4.0',
                acknowledged=0,
                execution_time=0.011084,
                is_flapping=0
            ))
        ]

    def test_canonical_command_with_nrpe(self):
        actual = nagios_exporter.canonical_command('check_nrpe2!check_node')

        self.assertEqual(actual, 'check_node')

    def test_connect_when_connection_fails(self):
        with self.assertRaises(nagios_exporter.NagiosConnectError):
            session = nagios_exporter.connect('/not-a-real-path')

    def test_livestatus_query(self):
        json_response = json.dumps(self.services)
        fixed16_header = fixed16('200', len(json_response))
        fake_sock = FakeSocketIO(fixed16_header + json_response)

        session = nagios_exporter.LiveStatus(fake_sock)
        actual = session.query('blah')

        self.assertEqual(actual, self.services)

    def test_livestatus_query_when_recv_response_is_empty(self):
        with self.assertRaises(nagios_exporter.NagiosResponseError):
            fake_sock = FakeSocketIO('')

            session = nagios_exporter.LiveStatus(fake_sock)
            session.query('blah')

    def test_livestatus_query_when_recv_raises_exception(self):
        class FakeSocketIOWithError(FakeSocketIO):
            """Subclass of FakeSocketIO that raises an exception on sendall"""

            def recv(self, count):
                raise socket.error('fake socket error')

        with self.assertRaises(nagios_exporter.NagiosResponseError):
            # Expect 10 bytes that are never sent.
            fake_sock = FakeSocketIOWithError(fixed16('400', 10))

            session = nagios_exporter.LiveStatus(fake_sock)
            session.query('blah')

    def test_livestatus_query_when_response_code_is_error(self):
        with self.assertRaises(nagios_exporter.NagiosResponseError):
            # Expect 10 bytes that are never sent.
            message = 'error message'
            fake_sock = FakeSocketIO(fixed16('400', len(message)) + message)

            session = nagios_exporter.LiveStatus(fake_sock)
            session.query('blah')

    def test_livestatus_query_when_sendall_raises_exception(self):
        class FakeSocketIOWithError(FakeSocketIO):
            """Subclass of FakeSocketIO that raises an exception on sendall"""

            def sendall(self, msg):
                raise socket.error('fake socket error')

        with self.assertRaises(nagios_exporter.NagiosQueryError):
            # Text is never used.
            fake_sock = FakeSocketIOWithError('')

            session = nagios_exporter.LiveStatus(fake_sock)
            session.query('blah')

    def test_get_services(self):
        expected = [
            'nagios_check_load_exec_time{hostname="localhost", service="Current Load"} 0.011084',
            'nagios_check_load_latency{hostname="localhost", service="Current Load"} 0.078',
            'nagios_check_load_state{hostname="localhost", service="Current Load"} 0',
            'nagios_check_load_flapping{hostname="localhost", service="Current Load"} 0',
            'nagios_check_load_acknowledged{hostname="localhost", service="Current Load"} 0'
        ]
        json_response = json.dumps(self.services)
        fixed16_header = fixed16('200', len(json_response))
        fake_sock = FakeSocketIO(fixed16_header + json_response)

        session = nagios_exporter.LiveStatus(fake_sock)
        actual = nagios_exporter.get_services(session, False, ())

        self.assertEqual(actual, expected)

    def test_get_status(self):
        expected = [
            'nagios_thing_a 1',
            'nagios_thing_b 0'
        ]
        json_response = json.dumps([['thing_a', 'thing_b'], [1, 0]])
        fixed16_header = fixed16('200', len(json_response))
        fake_sock = FakeSocketIO(fixed16_header + json_response)

        session = nagios_exporter.LiveStatus(fake_sock)
        actual = nagios_exporter.get_status(session)

        self.assertItemsEqual(actual, expected)

    def test_parse_args(self):
        args = nagios_exporter.parse_args(['--path', '/some/path'])

        self.assertEqual(args.path, '/some/path')

    def test_collect_metrics_with_bad_path(self):
        args = nagios_exporter.parse_args(['--path', '/not-a-real/path'])

        lines = []
        nagios_exporter.collect_metrics(args, lines)

        self.assertIn('nagios_livestatus_available 0', lines)

    def test_format_metric_with_various_value_types(self):
      # Integer.
      self.assertEqual(
          'nagios_check_cmd{key="/"} 1',
          nagios_exporter.format_metric('check_cmd', {'key': '/'}, '1'))
      # Float.
      self.assertEqual(
          'nagios_check_cmd{key="/"} 0.1',
          nagios_exporter.format_metric('check_cmd', {'key': '/'}, '0.1'))
      # String.
      self.assertEqual(
          'nagios_check_cmd{value="v0.1", key="/"} 1',
          nagios_exporter.format_metric('check_cmd', {'key': '/'}, 'v0.1'))

    @mock.patch.object(nagios_exporter, 'collect_metrics')
    def test_metrics_when_exception_is_raised(self, mock_metrics):
        mock_metrics.side_effect = nagios_exporter.NagiosResponseError('error')

        actual = nagios_exporter.metrics(())

        self.assertEqual(actual.status, '200 OK')
        self.assertEqual(actual.get_data(), 'nagios_exporter_success 0\n')

    @mock.patch.object(nagios_exporter, 'collect_metrics')
    def test_metrics(self, mock_metrics):
        actual = nagios_exporter.metrics(())

        self.assertEqual(actual.status, '200 OK')
        self.assertEqual(actual.get_data(), 'nagios_exporter_success 1\n')

    @mock.patch.object(nagios_exporter, 'connect')
    @mock.patch.object(os.path, 'exists')
    def test_collect_metrics_when_all_metrics_is_true(
        self, mock_exists, mock_connect):
        args = nagios_exporter.parse_args(
            ['--path', '/not-a-real/path', '--all_metrics', '--perf_data'])
        expected_status = [
            'nagios_livestatus_available 1',
            'nagios_thing_a 1',
            'nagios_thing_b 0'
        ]
        # Setup fake get_status response.
        json_response = json.dumps([['thing_a', 'thing_b'], [1, 0]])
        fixed16_header = fixed16('200', len(json_response))
        fake_status_sock = FakeSocketIO(fixed16_header + json_response)
        expected_services = [
            'nagios_check_load_exec_time{hostname="localhost", service="Current Load"} 0.011084',
            'nagios_check_load_latency{hostname="localhost", service="Current Load"} 0.078',
            'nagios_check_load_state{hostname="localhost", service="Current Load"} 0',
            'nagios_check_load_flapping{hostname="localhost", service="Current Load"} 0',
            'nagios_check_load_acknowledged{hostname="localhost", service="Current Load"} 0',
            'nagios_check_load_perf_data_value{hostname="localhost", service="Current Load", key="load1"} 0.560'
        ]
        # Setup fake get_services response.
        json_response = json.dumps(self.services)
        fixed16_header = fixed16('200', len(json_response))
        fake_service_sock = FakeSocketIO(fixed16_header + json_response)
        mock_exists.return_value = True
        mock_connect.side_effect = [fake_status_sock, fake_service_sock]

        values = []
        nagios_exporter.collect_metrics(args, values)

        self.assertItemsEqual(values, expected_status + expected_services)

    @mock.patch.object(nagios_exporter, 'connect')
    @mock.patch.object(os.path, 'exists')
    def test_collect_metrics_when_whitelist(
        self, mock_exists, mock_connect):
        args = nagios_exporter.parse_args(
            ['--path=/not-a-real/path', '--whitelist=nagios_check_load_state'])
        expected_status = [
            'nagios_livestatus_available 1',
            'nagios_thing_a 1',
            'nagios_thing_b 0'
        ]
        # Setup fake get_status response.
        json_response = json.dumps([['thing_a', 'thing_b'], [1, 0]])
        fixed16_header = fixed16('200', len(json_response))
        fake_status_sock = FakeSocketIO(fixed16_header + json_response)
        expected_services = [
            'nagios_check_load_state{hostname="localhost", service="Current Load"} 0',
        ]
        # Setup fake get_services response.
        json_response = json.dumps(self.services)
        fixed16_header = fixed16('200', len(json_response))
        fake_service_sock = FakeSocketIO(fixed16_header + json_response)
        mock_exists.return_value = True
        mock_connect.side_effect = [fake_status_sock, fake_service_sock]

        values = []
        nagios_exporter.collect_metrics(args, values)

        self.assertItemsEqual(values, expected_status + expected_services)

    @mock.patch.object(socket, 'socket')
    def test_connect(self, mock_socket):
        mock_conn = mock.Mock()
        mock_socket.return_value = mock_conn

        nagios_exporter.connect('/fake-path')

        self.assertEqual(mock_socket.call_count, 1)
        self.assertEqual(mock_conn.connect.call_count, 1)

    def test_parse_value_and_unit(self):
        self.assertEqual(
            nagios_exporter.parse_value_and_unit('2400MB'), ('2400', 'MB'))
        self.assertEqual(
            nagios_exporter.parse_value_and_unit('30%'), ('30', '%'))
        self.assertEqual(
            nagios_exporter.parse_value_and_unit('0.323ms'), ('0.323', 'ms'))
        self.assertEqual(
            nagios_exporter.parse_value_and_unit('3.4'), ('3.4', ''))
        self.assertEqual(
            nagios_exporter.parse_value_and_unit('v0.3.4'), ('v0.3.4', ''))

    def test_convert_value_to_base_unit(self):
        # Known unit.
        self.assertEqual(
            nagios_exporter.convert_value_to_base_unit('2400', 'KB'),
            '2457600.0')
        # No unit.
        self.assertEqual(
            nagios_exporter.convert_value_to_base_unit('2400', ''), '2400')
        # Ignored / unknown unit.
        self.assertEqual(
            nagios_exporter.convert_value_to_base_unit('2400', 'hz'), '2400')
        # Not a numeric value.
        self.assertEqual(
            nagios_exporter.convert_value_to_base_unit('v3.1', ''), 'v3.1')
        # Not a numeric value with a known unit (e.g. coincidence or bad value)
        self.assertEqual(
            nagios_exporter.convert_value_to_base_unit('v3.1', 'KB'), 'v3.1KB')

    def test_get_perf_data(self):
        expected = [
            ('check_disk_perf_data_used', {'key': '/'}, '2516582400.0'),
            ('check_disk_perf_data_free', {'key': '/'}, '50704941056.0'),
            ('check_disk_perf_data_total', {'key': '/'}, '63381176320.0')
        ]

        actual = nagios_exporter.get_perf_data(
            'check_disk', {},
            ['/=2400MB;48356;54400;0;60445'],
            ['check_disk=used;free;;;total'])

        self.assertItemsEqual(actual, expected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
