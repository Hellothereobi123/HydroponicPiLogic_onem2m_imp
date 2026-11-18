"""oneM2M support: a small HTTP-binding client, resource provisioning, and the readings uploader.

The Pi is both a CSE (ACME, configured in acme/acme.ini) and this AE, which registers with it.
The AE can also point at a remote CSE instead (ONEM2M_CSE_URL), e.g. an IN-CSE on EC2.
See docs/onem2m.md.

How a reading gets from a sensor to the app (HYDRO_BACKEND=onem2m):

    main.py (hydro-sensors.service)
        every 30 s: read sensors -> db.insert_cycle() -> one row in SQLite, synced_onem2m = 0
        (no network here, so a slow or missing CSE never delays sampling)

    onem2m_sync.py (hydro-onem2m.service) -> uploader.run(), forever:
        1. provision.provision()   register the AE, then make sure its ACP, "readings"
                                   timeSeries and "vision" container exist (safe to repeat)
        2. uploader.upload_pending()
              db.get_unsynced_readings(column="synced_onem2m")   oldest first, one batch
              for each row: client.create(timeSeriesInstance, dgt = cycle time,
                                          con = config.remote_record(row))
              db.mark_synced(...)   only rows the CSE accepted (or already had)
        3. sleep, then back to 2. If the CSE is down: back off and retry. If our resources
           have vanished (404): go back to 1.

    camera.py -> push_metrics() -> uploader.publish_vision()
        provisions (cheap), then adds one contentInstance to "vision"

    On-demand requests (ondemand.serve_requests, every pass of the same loop, ~3 s):
        the app posts a request into "requests"; the Pi answers it from SQLite into
        "responses" (labelled rqi:<id>) and deletes the request

    The app (read-only) discovers AEs labelled "type:hydropi", then reads each AE's
    "readings" for a recent time window and "vision/la" (the latest metrics).

Modules:
    client.py      HTTP requests with oneM2M headers; returns the RSC, raises CSEUnreachable
    provision.py   creates the resource tree under the CSEBase
    uploader.py    the upload loop (run), one batch (upload_pending), camera metrics
    ondemand.py    answers the app's on-demand requests (latest, history) from SQLite
    make_token.py  bearer tokens and the hashes ACME's token file needs
"""
