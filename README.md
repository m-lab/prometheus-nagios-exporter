[![Build Status](https://travis-ci.org/m-lab/prometheus-nagios-exporter.svg?branch=master)](https://travis-ci.org/m-lab/prometheus-nagios-exporter)
[![Coverage Status](https://coveralls.io/repos/github/m-lab/prometheus-nagios-exporter/badge.svg?branch=master)](https://coveralls.io/github/m-lab/prometheus-nagios-exporter?branch=master)

# prometheus-nagios-exporter

The Prometheus Nagios exporter reads status and performance data from nagios
plugins via the [MK Livestatus][livestatus] Nagios plugin and publishes this
in a form that can be scrapped by Prometheus.

[livestatus]: https://mathias-kettner.de/checkmk_livestatus.html

# Setup

Setup is as simple as installing the livestatus module and then running the
`nagios_exporter.py` service.

    echo 'broker_module=/usr/lib/check_mk/livestatus.o /var/lib/nagios3/rw/livestatus' >> /etc/nagios3/nagios.cfg
    echo 'event_broker_options=-1' >> /etc/nagios3/nagios.cfg

Restart Nagios, and start the exporter:

    ./nagios_exporter.py --path /var/lib/nagios3/rw/livestatus

It should then be possible to visit:

    http://localhost:5000/metrics

# Metrics

Every metric is prefixed with `nagios_`, following the [metric naming best
practices][naming]. The prefix is followed by the name of the Nagios check
command, such as `nagios_check_load_`. The metric name suffix comes from various
Nagios status names. For example, a load service check for `localhost` would
include the following metrics:

```
nagios_check_load_exec_time{hostname="localhost", service="Load"} 0.011084
nagios_check_load_latency{hostname="localhost", service="Load"} 0.078
nagios_check_load_state{hostname="localhost", service="Load"} 0
nagios_check_load_flapping{hostname="localhost", service="Load"} 0
nagios_check_load_acknowledged{hostname="localhost", service="Load"} 0
```

Every metric is also labeled with the hostname and service description.

[naming]: https://prometheus.io/docs/practices/naming/

# Performance data

Performance data is plugin-specific. Though there is a common format that most
plugins follow. Performance data follows plugin output starting with `|`.
Typically, the format is a set of `key=value1[;value2]+` strings. For example:

```
$ check_disk <some args>
DISK OK - free space: / 2400 MB (69% inode=83%);| /=2400MB;48356;54400;0;60445
```

By default, nagios-exporter only parses the first value of performance data
for every key. The default field name is 'value'. And, the key is always added
as a metric label. So, for example, the default metric output for the above
performance data would be:

```
nagios_check_disk_perf_data_value{key="/", ...} 2516582400.0
```

More specific names can be assigned to each value position of particular check
plugins using the `--data names` flag. For example:

```
--data_names="check_disks=used;free;;;total"
```

So, instead of only parsing the first value and using the default name, now the
metric output for the original example will include three values each named and
corresponding to the respective value in the raw perf data:

```
nagios_check_all_disks_perf_data_used{key="/", ...}  2516582400.0
nagios_check_all_disks_perf_data_free{key="/", ...} 50704941056.0
nagios_check_all_disks_perf_data_totalkey="/", ...} 63381176320.0
```

# Example

```
    ./nagios_exporter.py --path /var/lib/nagios3/rw/livestatus \
        --perf_data --perf_names="check_disks=used;free;;;total" \
        --whitelist nagios_check_all_disks_perf_data
```
