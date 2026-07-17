# This file is part of the pychess-variants fairyfishnet client.
# Copyright (C) 2016-2019 Niklas Fiekas <niklas.fiekas@backscattering.de>
# Copyright (C) 2019 Bajusz Tamás <gbtami@gmail.com>
# SPDX-License-Identifier: GPL-3.0-or-later

"""HTTP response and version-comparison helpers."""

import logging
import re
import urllib.parse as urlparse

from .errors import JsonResponseError


def base_url(url):
    url_info = urlparse.urlparse(url)
    return "%s://%s/" % (url_info.scheme, url_info.hostname)


def response_body_snippet(response, limit=300):
    try:
        text = response.text
    except Exception:
        return "<unreadable response body>"

    text = text.replace("\r", "\\r").replace("\n", "\\n")
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def response_json(response, context):
    try:
        return response.json()
    except ValueError as err:
        content_type = response.headers.get("Content-Type", "-")
        raise JsonResponseError(
            "%s returned invalid JSON (HTTP %s %s, content-type %s): %s"
            % (context, response.status_code, response.reason, content_type, response_body_snippet(response))
        ) from err


def release_file_url(files):
    for file_info in files:
        if file_info.get("packagetype") == "bdist_wheel":
            return file_info["url"]
    return files[0]["url"]


def version_key(version):
    parts = []
    for part in re.split(r"[.+_-]", version):
        match = re.match(r"(\d+)", part)
        if not match:
            break
        parts.append(int(match.group(1)))
    return tuple(parts)


def is_newer_version(candidate, current):
    candidate_key = version_key(candidate)
    current_key = version_key(current)
    if not candidate_key or not current_key:
        logging.warning(
            "Could not compare versions %s and %s; skipping auto update",
            candidate,
            current,
        )
        return False

    width = max(len(candidate_key), len(current_key))
    candidate_key += (0,) * (width - len(candidate_key))
    current_key += (0,) * (width - len(current_key))
    return candidate_key > current_key
