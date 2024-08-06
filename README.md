# HMPPS Health Ping

This app fetches a list of all microservices/environments (components) from the Service Catalogue API. At a regular interval (default 2 mins) it performs a http GET on all the `/health` and `/info` endpoints for each microservice. The results and any errors are recorded into a redis stream. The raw json output from the endpoints is also stored. In addition, the application version is also gathered and added to a redis stream containing a version history.

The redis stream data can be queried to create dashboards to display service health status/history. See https://github.com/ministryofjustice/hmpps-developer-portal/

Redis stream keys follow this naming pattern:

```sh
info:[component name]:[environment name]
health:[component name]:[environment name]
version:[component name]:[environment name]
```

# How to run locally

- Ensure python is installed: `brew install python`
- Create local env: `python3 -m venv .python`
- Ensure all dependencies are installed: `.python/bin/pip install -r requirements.txt`  
- Copy and create .env file: `cp .env.example .env`
- Either use a local redis by: `docker compose up -d`
- Or port-forward to redis in the dev environment via kubectl:
```sh
kubectl -n hmpps-portfolio-management-dev port-forward port-forward-pod 6379:6379
```

Run: `export $(cat .env) &&  .python/bin/python health_ping.py`
