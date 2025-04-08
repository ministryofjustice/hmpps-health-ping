# HMPPS Health Ping

This app fetches a list of all microservices/environments (components) from the Service Catalogue API. At a regular interval (default 2 mins) it performs a http GET on all the `/health` and `/info` endpoints for each microservice. The results and any errors are recorded into a redis stream. The raw json output from the endpoints is also stored. In addition, the application version is also gathered and added to a redis stream containing a version history.

The redis stream data can be queried to create dashboards to display service health status/history. See https://github.com/ministryofjustice/hmpps-developer-portal/

**Note:** Because redis is an in-memory database, if the instance is restarted, the data (including, for example, version history) will be lost.

## Redis streams

Redis stream keys follow this naming pattern:

Individual streams per component/environment:
```
health:[component name]:[environment name]
info:[component name]:[environment name]
version:[component name]:[environment name]
```

### health
The health stream is a direct dump of the output of the URI within the `health` field of the Service Catalogue entry.

It is updated each time the script is run. THere is a maximum of MAX_STREAM_LENGTH (default 360) entries

eg.
```
XRANGE 'health:hmpps-digital-prison-reporting-mi:prod' - +
```

### info
The info stream contains direct dump of the output of the URI within the `info` field of the Service Catalogue entry.
It is updated each time the script is run. THere is a maximum of MAX_STREAM_LENGTH (default 360) entries

eg.
```
XRANGE 'info:hmpps-digital-prison-reporting-mi:prod' - +
```


### version
The version stream updates whenever the version of the application changes. This also updates the `build_image_tag` in the Service catalogue.

eg.
```
XRANGE 'version:hmpps-digital-prison-reporting-mi:prod' - +
```

## Redis JSON

These are JSON documents representing the latest state of components and their corresponding environemtn
```
latest:health
latest:info
latest:version
```

### latest:health
This contains the latest entry for the health of each application and environment. It is updated with the result of the most recent health endpoint call, and is used to determine the UP/DOWN state in the [monitor](https://developer-portal.hmpps.service.justice.gov.uk/monitor) page of the Developer Portal.

eg.
```
JSON.GET latest:health $.health:hmpps-digital-prison-reporting-mi:prod
```

### latest:versions
This contains the latest entry for the version of the application. It is updated whenever a valid version of the application is returned either from the /info or /health endpoints, and is used to determine the Build version in the [monitor](https://developer-portal.hmpps.service.justice.gov.uk/monitor) page of the Developer Portal.
eg.
```
JSON.GET latest:versions $.version:hmpps-digital-prison-reporting-mi:prod
```

### latest:info
This contains the latest entry for the info about each application and environment. It is updated with the result of the most recent info endpoint call.

eg.
```
JSON.GET latest:versions $.version:hmpps-digital-prison-reporting-mi:prod
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

## How to connect to Redis locally

You will need the *AUTH* token to authenticate to the Redis instance. 

- Install redis-cli: `brew install redis`
- Create a port forward pod to the development redis instance (check to see if there's already one using `kubectl get pods -n hmpps-portfolio-management-dev`)

>```bash
> kubectl \
>   -n hmpps-portfolio-management-dev \
>   run port-forward-pod \
>   --image=ministryofjustice/port-forward \
>   --port=6379 \
>   --env="REMOTE_HOST=[redis host]" \
>  --env="LOCAL_PORT=6379" \
> --env="REMOTE_PORT=6379"
> ```

- Use kubectl to port-forward to it:

> ```bash
> kubectl \
>   -n hmpps-portfolio-management-dev \
>   port-forward \
>   port-forward-pod 6379:6379
> ```

- Use the redis-cli to open a connection

> ```bash
> redis-cli --tls  -p 6379
>```

- Authenticate using AUTH
> ```redis
> AUTH _PASTE_THE_TOKEN_HERE_
> ```
