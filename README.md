# swf-monitor

Monitoring and information service for the ePIC streaming workflow testbed. For information
on the testbed and its software see the umbrella repository for the testbed [swf-testbed](https://github.com/bnlnpps/swf-testbed).

This is a web service providing system monitoring and comprehensive
information about the testbed's state, both via browser-based dashboards and a
json based REST API.

This module will manage the databases used by the testbed, and offer a REST API
for other agents in the system to report status and retrieve information.

## Implementation notes

- Django-based web service providing a browser UI and a REST json API,
  leveraging the PanDA monitor.
- Postgres as the back end database, as for PanDA and Rucio.
- Receives information from agents in the system via REST or ActiveMQ
  messages.
- Interfaces with OpenSearch/Grafana for monitoring dashboards.

