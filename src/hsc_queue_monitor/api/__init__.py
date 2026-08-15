"""Read-only experiments against the HSC JSON API.

This package exists to answer one question: can availability eventually be
*read* over HTTP instead of by clicking through the wizard? Nothing in here is
part of the monitor, the queue flow, the booking flow or the availability
scanner, and nothing in here books, reserves or submits anything.

Five pieces, deliberately small:

* :mod:`.probe`         copy the browser's HSC cookies into a ``requests.Session``
                        and issue one GET, reporting what came back and which
                        cookies the site changed;
* :mod:`.endpoints`     the list of API calls that have actually been *measured*
                        from browser traffic — no guessed URLs live here;
* :mod:`.observer`      safe metadata about the ``/api/`` calls the real page
                        makes while a human clicks, so the list above can grow
                        from evidence;
* :mod:`.client`        the three measured endpoints, GET only, sharing one
                        session so the site's own cookie updates carry forward;
* :mod:`.availability`  departments -> days -> slots for one centre, parsed
                        without guessing a field name.
"""

from __future__ import annotations

from .availability import (
    ApiScan,
    ApiSchemaUnknown,
    Department,
    DepartmentUnresolved,
    parse_days,
    parse_slots,
    render_api_availability,
    resolve_department,
    scan_centre,
)
from .bootstrap import QueueBootstrap, wizard_fingerprint
from .client import (
    DEFAULT_SERVICE_ID,
    PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A,
    ApiCall,
    ApiRequestFailed,
    HscApiClient,
    client_for,
)
from .endpoints import MEASURED_REQUESTS, MeasuredRequest
from .observer import ApiObserver, ApiRecord
from .probe import (
    API_HOST,
    API_ORIGIN,
    DEFAULT_PATH,
    CookieChange,
    CookieInfo,
    ProbeOutcome,
    build_session,
    describe_cookies,
    hsc_cookies,
    is_hsc_cookie_domain,
    perform,
    render_outcome,
    resolve_url,
)

__all__ = [
    "API_HOST",
    "API_ORIGIN",
    "DEFAULT_PATH",
    "DEFAULT_SERVICE_ID",
    "MEASURED_REQUESTS",
    "PRACTICAL_EXAM_SERVICE_CENTER_VEHICLE_CATEGORY_A",
    "ApiCall",
    "ApiObserver",
    "ApiRecord",
    "ApiRequestFailed",
    "ApiScan",
    "ApiSchemaUnknown",
    "CookieChange",
    "CookieInfo",
    "Department",
    "DepartmentUnresolved",
    "HscApiClient",
    "MeasuredRequest",
    "ProbeOutcome",
    "QueueBootstrap",
    "build_session",
    "client_for",
    "describe_cookies",
    "hsc_cookies",
    "is_hsc_cookie_domain",
    "parse_days",
    "parse_slots",
    "perform",
    "render_api_availability",
    "render_outcome",
    "resolve_department",
    "resolve_url",
    "scan_centre",
    "wizard_fingerprint",
]
