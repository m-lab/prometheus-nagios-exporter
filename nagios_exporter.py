#!/usr/bin/python
"""prometheus-nagios-exporter uses the Nagios livestatus plugin to report
on current Nagios service status for collection by Prometheus.
"""

import argparse
import contextlib
import collections
import json
import logging
import os
import re
import socket
import sys

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

# Maps known units to a scaling factor for converting Nagios performance data
# into canonical units for prometheus.
UNIT_TO_SCALE = {
    'GB': 1024 * 1024 * 1024,
    'MB': 1024 * 1024,
    'KB': 1024,
    'ms': 0.001,
    'usec': 0.000001,
    '%': 0.01,
}

# Extract performance data value and unit, e.g. 2400MB, 2.3%.
UNIT_REGEX = re.compile('([0-9.]+)([^0-9.]+)?')

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

    parser = argparse.ArgumentParser(
        description='Prometheus Nagios Exporter.')
    parser.add_argument(
        '--path', help='Absolute path to livestatus Nagios UNIX socket.')

    # TODO: choose appropriate port based on:
    # https://github.com/prometheus/prometheus/wiki/Default-port-allocations
    parser.add_argument(
        '--port', type=int, default=5000,
        help='Export server should listen on port.')

    # Report metrics in the "--whitelist" or "--all_metrics".
    parser.add_argument(
        '--whitelist', default=None, action='append',
        help=('Default: only export metrics for services that include this '
              'whitelist pattern. Can be specified multiple times.'))
    parser.add_argument(
        '--all_metrics', default=False, action='store_true',
        help=('Instead of a select whitelist of metrics, always report all '
              'metrics.'))

    # The default is to start the exporter as an HTTP service. Alternately, dump
    # metrics once to stdout.
    parser.add_argument(
        '--dump_metrics', dest='dump_metrics', action='store_true',
        help=('Writes all metrics to stdout and then exits. Useful for choosing '
              'metrics for whitelist selection.'))

    # Generate metrics from the nagios performance data where available.
    parser.add_argument(
        '--perf_data', dest='use_perf_data', default=False, action='store_true',
        help='Generate metrics for performance data.')
    parser.add_argument(
        '--data_names', default=[], dest='data_names',
        action='append',
        help=('When parsing additional fields than the first, specify: '
              '--data_names=<check_cmd>=<0th-name>;<1st-name>;;;'))

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


def parse_perf_data_fields(raw_perf_data):
    """Parses raw performance data or data names flags.

    Args:
        raw_perf_data: iterable of str, each element is a key=values
    Returns:
        dict of str to list of fields.

    For example:
        --data_names="check_all_disks=used;free"
        --data_names="check_load=load"

    Becomes raw_perf_data:
        ["check_all_disks=used;free", "check_load=load"]

    Becomes value_map:
        {"check_all_disks": ['used', 'free'], "check_load": ['load']}
    """
    fields = {}
    for raw_value in raw_perf_data:
        name, values = raw_value.split('=')
        values = values.split(';')
        fields[name] = values
    return fields


def get_perf_data(check_command, metric_labels, raw_perf_data_values,
                  raw_perf_data_names):
    """Parses raw performance data from check plugins for prometheus metrics.

    By default, only the first performance data value from every key is used.

    If the key name contains characters [a-zA-Z0-9] then that is used literally
    in the prometheus metric name. For keys with additional characters, the
    prometheus metric uses a generic name "_perf_value" with a "key=" label
    equal to the key name.

    For example, a check_load performance data would include a load1 value:

      load1=0.000;5.000;10.000;0;

    Which parse_perf_data would translate to:

      nagios_check_load_perf_data{key="load1", ...} 0.000

    Whereas, a check_disk performance data would include a filesystem path as
    the key name, e.g:

      /=2400MB;48356;54400;0;60445

    Which parse_perf_data would translate to:

      nagios_check_disk_perf_data{key="/", ...} 2400000000

    Args:
      check_command: str, the metric name prefix, used to create metric names
          for performance data.
      metric_labels: dict of str, key value labels to apply resulting metrics.
      raw_perf_data_values: iterable of str, the performance data as collected by
          Nagios from the check plugins.
      raw_perf_data_names: iterable of str, each element should match the
          pattern: <check_command>=<0-name>[;<1-name>]*. This names perf_data
          values for the given check_command. The default field name is simply
          'value'.

    Returns:
      list of (suffix, labels, value) tuples.

    Examples:
      load1=0.000;5.000;10.000;0; /=2400MB;48356;54400;0;60445
    """
    metrics = []

    values_map = parse_perf_data_fields(raw_perf_data_values)
    names_map = parse_perf_data_fields(raw_perf_data_names)

    for key, raw_values in values_map.iteritems():
        labels = {'key': key}
        labels.update(metric_labels)

        # NOTE: Units are typically only noted on the first value, so save it.
        _, unit = parse_value_and_unit(raw_values[0])

        # Use given field names, or default to use the first value only.
        field_names = names_map.get(check_command, ('value',))

        # Convert every perf_data value for which we have a field name.
        for i, field_name in enumerate(field_names):
            if not field_name:
                continue

            value, _ = parse_value_and_unit(raw_values[i])
            base_value = convert_value_to_base_unit(value, unit)
            suffix = '_' + field_name
            metrics.append(
                (check_command + '_perf_data' + suffix, labels, base_value))

    return metrics


def parse_value_and_unit(raw_value):
    """Returns the value, unit tuple. Unit may be empty."""
    m = UNIT_REGEX.match(raw_value)
    if not m:
        return raw_value, ''
    value, unit = m.groups('')
    return value, unit


def convert_value_to_base_unit(value, unit):
    """Converts value to canonical units."""
    if not unit:
        return value

    if unit not in UNIT_TO_SCALE:
        # TODO: log missing unit.
        return value

    try:
        value = float(value)
        value *= UNIT_TO_SCALE[unit]
    except ValueError:
        # Leave value as a string. e.g. 0.3.1
        return value + unit
    return str(value)


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


def get_services(session, use_perf_data, raw_perf_data_names):
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

        if use_perf_data and s.perf_data:
            values = get_perf_data(
                cmd, labels, s.perf_data.split(), raw_perf_data_names)
            for (perf_metric, perf_labels, value) in values:
                lines.append(format_metric(perf_metric, perf_labels, value))

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
        session = LiveStatus(sock)
        services = get_services(
            session, args.use_perf_data, args.data_names)

    if args.whitelist:
        for whitelist in args.whitelist:
            lines.extend(filter(lambda x: whitelist in x, services))
    elif args.all_metrics or args.dump_metrics:
        lines.extend(services)

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
    if args.dump_metrics:
        resp = metrics(args)
        sys.stdout.write(resp.get_data())
        sys.exit(0)

    app = flask.Flask(__name__)
    app.add_url_rule('/metrics', 'metrics', lambda: metrics(args))
    app.run(host='0.0.0.0', port=args.port, debug=True)


if __name__ == '__main__':  # pragma: no cover
    main()
