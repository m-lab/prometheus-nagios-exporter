[![Build Status](https://travis-ci.org/m-lab/prometheus-nagios-exporter.svg?branch=master)](https://travis-ci.org/m-lab/prometheus-nagios-exporter)
[![Coverage Status](https://coveralls.io/repos/github/m-lab/prometheus-nagios-exporter/badge.svg?branch=master)](https://coveralls.io/github/m-lab/prometheus-nagios-exporter?branch=master)

# prometheus-nagios-exporter

The prometheus nagios exporter reads status and performance data from nagios
plugins via the [MK Livestatus][livestatus] nagios plugin and publishes this
in a form that can be scrapped by prometheus.

[livestatus]: https://mathias-kettner.de/checkmk_livestatus.html

# Setup

Setup is as simple as installing the livestatus module and then running the
prometheus-nagios-exporter.py service.

    echo 'broker_module=/usr/lib/check_mk/livestatus.o /var/lib/nagios3/rw/livestatus' >> /etc/nagios3/nagios.cfg
    echo 'event_broker_options=-1' >> /etc/nagios3/nagios.cfg

Restart nagios, and start the exporter:

    ./prometheus-nagios-exporter.py --path /var/lib/nagios3/rw/livestatus

# Metrics

Every metric is prefixed with `nagios_`, following the [metric naming best
practices][naming]. The prefix is followed by the name of the nagios check
command, such as `nagios_check_load_`. The metric name suffix comes from various
nagios status names. For example, a load service check for `localhost` would
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

For better or worse, performance data is plugin-specific. Though there is a
general format that most plugins follow.

TODO(soltesz): add details.
