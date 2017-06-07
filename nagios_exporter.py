#!/usr/bin/python
"""prometheus-nagios-exporter uses the Nagios livestatus plugin to report
on current Nagios service status for collection by Prometheus.
"""

import argparse
import contextlib
import collections
import json
import os
import sys
import socket

import flask


# Wait no more than MAX_SOCKET_WAIT seconds for livestatus communication.
MAX_SOCKET_WAIT = 15

# Column names roughly correspond to nagios configuration names. Some names
# are defined by the livestatus plugin.
COLUMNS = [
    'host_name', 'service_description', 'state', 'latency', 'perf_data',
    'process_performance_data', 'check_command', 'acknowledged',
    'execution_time', 'is_flapping'
]

# Service contains named fields corresponding to the column names returned by
# the livestatus plugin.
Service = collections.namedtuple('Service', ' '.join(COLUMNS))


class NagiosError(Exception):
    """Base class for errors."""


class NagiosResponseError(NagiosError):
    """The livestatus plugin failed to respond."""


class NagiosQueryError(NagiosError):
    """The livestatus query failed."""


class NagiosConnectError(NagiosError):
    """Connecting to the livestatus plugin failed."""


def parse_args(args):
    """Parses command line arguments."""

    parser = argparse.ArgumentParser(description='Prometheus Nagios Exporter.')
    parser.add_argument(
        '--path', help='Absolute path to livestatus Nagios UNIX socket.')

    # TODO: choose appropriate port based on:
    # https://github.com/prometheus/prometheus/wiki/Default-port-allocations
    parser.add_argument(
        '--port', type=int, default=5000,
        help='Export server should listen on port.')

    # TODO: support --whitelist patterns to limit exported services.
    parser.add_argument(
        '--whitelist', default=None, help=('Only export metrics for services '
            'that include this whitelist pattern.'))

    return parser.parse_args(args)


def connect(path):
    """Connects to unix sock at the given path."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(MAX_SOCKET_WAIT)
        sock.connect(path)
    except socket.error as err:
        raise NagiosConnectError(err)
    return sock


class LiveStatus(object):
    """LiveStatus manages queries to the livestatus plugin."""

    def __init__(self, sock):
        self._sock = sock

    def query(self, query):
        """Queries the livestatus plugin.

        The OutputFormat and ResponseHeader is managed by the LiveStatus
        class, so do not include these directives in given queries.

        Args:
          query: str, a livestatus query.

        Returns:
          list of lists.

        Raises:
          NagiosQueryError: a send error.
          NagiosResponseError: a receive error.

        Example queries:
           "GET services\nColumns: host_name"
        """
        # Extend query to use JSON format and fixed16 header.
        query += '\nOutputFormat: json\nResponseHeader: fixed16\n'
        self._send(query)

        # Signal to livestatus that the query is complete.
        self._sock.shutdown(socket.SHUT_WR)

        # Read the 'fixed16' header: "<status code> <response length>\n"
        header = self._receive(16)

        # For error codes, there is still a message explaining the error.
        code, length = header.split()
        length = int(length)
        data = self._receive(length)

        if code == '200':
            return json.loads(data)

        # Data contains the error message.
        raise NagiosResponseError(
            'Livestatus error: %s: %s' % (code, data.strip()))

    def _send(self, msg):
        """Sends the given message to the socket."""
        try:
            self._sock.sendall(msg)
        except socket.error as err:
            raise NagiosQueryError(err)

    def _receive(self, count):
        """Reads data from the livestatus plugin."""
        results = []
        while count > 0:
            try:
                data = self._sock.recv(count)
            except socket.error as err:
                raise NagiosResponseError(err)
            if len(data) == 0:
                msg = 'Failed to read data from nagios server.'
                raise NagiosResponseError(msg)
            count -= len(data)
            results.append(data)
        return ''.join(results)


def canonical_command(cmd):
    """Handles nrpe commands to return the canonical command name.

    Examples:
      check_load!5.0!4.0!3.0!10.0!6.0!4.0
      check_nrpe2!check_node
    """
    fields = cmd.split('!')
    if fields[0] == 'check_nrpe2':
        return fields[1]
    else:
        return fields[0]


def format_labels(labels):
    """Formats key=values to a prometheus label format."""
    if not labels:
        return ''

    fields = []
    for key, value in labels.iteritems():
        fields.append('%s="%s"' % (key, value))

    return '{' + ', '.join(fields) + '}'


def format_metric(name, labels, value):
    """Formats the prometheus metric."""
    return 'nagios_%s%s %s' % (
        name.replace('-', '_'), format_labels(labels), value)


def get_status(session):
    """Queries the livestatus plugin and exports status metrics about nagios."""
    query = 'GET status'
    status = session.query(query)
    values = dict(zip(status[0], status[1]))

    lines = []
    for key, value in values.iteritems():
        lines.append(format_metric(key, '', value))

    return lines


def get_services(session):
    """Queries the livestatus plugin and exports service metrics."""
    query = 'GET services\nColumns: ' + ' '.join(COLUMNS)
    services = [Service(*s) for s in session.query(query)]

    lines = []
    for s in services:
        # Standard labels.
        labels = {'hostname': s.host_name, 'service': s.service_description}

        cmd = canonical_command(s.check_command)
        # TODO: use a single histogram for all execution and latency times.
        lines.append(
            format_metric('%s_exec_time' % cmd, labels, s.execution_time))
        lines.append(
            format_metric('%s_latency' % cmd, labels, s.latency))
        lines.append(
            format_metric('%s_state' % cmd, labels, s.state))
        lines.append(
            format_metric('%s_flapping' % cmd, labels, s.is_flapping))
        lines.append(
            format_metric('%s_acknowledged' % cmd, labels, s.acknowledged))

        if s.process_performance_data:
            # TODO: parse s.perf_data.
            pass

    return lines


def collect_metrics(args, lines):
    """Generates metric data as a flask.Response."""
    if not os.path.exists(args.path):
        lines.append('# livestatus socket does not exist! %s' % args.path)
        lines.append('nagios_livestatus_available 0')
        return

    lines.append('nagios_livestatus_available 1')
    with contextlib.closing(connect(args.path)) as sock:
        session = LiveStatus(sock)
        lines.extend(get_status(session))

    with contextlib.closing(connect(args.path)) as sock:
        # TODO: support filtering specific services or hosts.
        session = LiveStatus(sock)
        lines.extend(get_services(session))

    return


def metrics(args):
    """Generates response to /metrics requests."""
    lines = []
    try:
        collect_metrics(args, lines)
        lines.append('nagios_exporter_success 1')
    except NagiosError:
        lines.append('nagios_exporter_success 0')

    # The last line must include a new line or the prometheus parser fails.
    response = '\n'.join(lines) + '\n'
    return flask.Response(response, content_type='text/plain; charset=utf-8')


def main():  # pragma: no cover
    args = parse_args(sys.argv[1:])
    app = flask.Flask(__name__)
    app.add_url_rule('/metrics', 'metrics', lambda: metrics(args))
    app.run(host='0.0.0.0', port=args.port, debug=True)


if __name__ == '__main__':  # pragma: no cover
    main()
