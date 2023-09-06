# HMPPS Health Ping

This app fetches a list of all microservices/environments (components) from the Service Catalogue API. At a regular interval (default 2 mins) it performs a http GET on all the `/health` and `/info` endpoints for each microservice. The results and any errors are recorded into a redis stream. The raw json output from the endpoints is also stored. In addition, the application version is also gathered and added to a redis stream containing a version history.

The redis stream data can be queried to create dashboards to display service health status/history. See https://github.com/ministryofjustice/hmpps-developer-portal/

Redis stream keys follow this naming pattern:

```sh
info:[component name]:[environment name]
health:[component name]:[environment name]
version:[component name]:[environment name]
```
