from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid as uuidlib
from typing import Any

import requests

from .config import Config, coerce_id
from .constants import HMAC_KEY, MEAL_LABELS, USER_AGENT
from .logging_utils import LOG


class ApiError(RuntimeError):
    """Unexpected HTTP/API response."""

    def __init__(self, message: str, status: int | None = None, body: Any = None):
        super().__init__(message)
        self.status = status
        self.body = body


class ProgresMealApi:
    """Small client that mirrors the official app's HTTP calls."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.webetu_token = ""
        self.uuid = ""
        self.annee_id: Any = None
        self.dia_id: Any = None
        self.wilaya: Any = cfg.wilaya
        self.residence: Any = cfg.residence
        self.meal_token = ""
        self.depots: list[dict[str, Any]] = []

    def _signature_headers(self, json_body: Any) -> dict[str, str]:
        data = "" if json_body is None else json.dumps(json_body, ensure_ascii=False, separators=(",", ":"))
        timestamp = str(int(time.time()))
        nonce = str(uuidlib.uuid4())
        message = f"{timestamp}|{nonce}|{data}"
        signature = hmac.new(
            HMAC_KEY.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "X-Timestamp": timestamp,
            "X-Nonce": nonce,
            "X-Signature": signature,
        }

    def _webetu_headers(self, token: str | None, json_body: Any = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["authorization"] = token
            if self.cfg.sign:
                headers.update(self._signature_headers(json_body))
        return headers

    @staticmethod
    def _decode(response: requests.Response) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            text = response.text
            return {"_raw": text[:1000]} if text else {}

    @staticmethod
    def _error_text(body: Any) -> str:
        if isinstance(body, dict):
            for key in ("error", "message", "detail"):
                value = body.get(key)
                if value:
                    if isinstance(value, (dict, list)):
                        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:600]
                    return str(value)
            data = body.get("data")
            if isinstance(data, dict):
                for key in ("error", "message", "detail"):
                    value = data.get(key)
                    if value:
                        if isinstance(value, (dict, list)):
                            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))[:600]
                        return str(value)
            if "_raw" in body:
                return str(body["_raw"])[:600]
        if isinstance(body, str):
            return body[:600]
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:600] if body else "empty response"

    def _request(
        self,
        method: str,
        url: str,
        *,
        label: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        retries: int = 0,
    ) -> tuple[int, Any]:
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")) if json_body is not None else None
        attempt = 0
        while True:
            try:
                LOG.debug("%s: %s %s", label, method, url.split("?", 1)[0])
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    params=params,
                    data=data,
                    timeout=self.cfg.timeout,
                )
                break
            except requests.RequestException as exc:
                if attempt < retries:
                    attempt += 1
                    delay = min(2 ** attempt, 10)
                    LOG.warning("%s: network error (%s); retrying in %ss...", label, exc, delay)
                    time.sleep(delay)
                    continue
                raise ApiError(f"{label}: network error: {exc}") from exc
        LOG.debug("%s: HTTP %s", label, response.status_code)
        body = self._decode(response)
        if response.status_code >= 400:
            raise ApiError(
                f"{label}: HTTP {response.status_code}: {self._error_text(body)}",
                response.status_code,
                body,
            )
        return response.status_code, body

    def webetu_get(self, path: str, label: str) -> Any:
        _, body = self._request(
            "GET",
            self.cfg.webetu_api + path,
            label=label,
            headers=self._webetu_headers(self.webetu_token, None),
            retries=self.cfg.retries,
        )
        return body

    def webetu_login(self) -> tuple[str, str]:
        payload = {"username": self.cfg.username, "password": self.cfg.password}
        _, body = self._request(
            "POST",
            self.cfg.webetu_api + "/authentication/v1/",
            label="WebEtu login",
            headers=self._webetu_headers(None, payload),
            json_body=payload,
            retries=self.cfg.retries,
        )
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(body, dict) and (body.get("token") or body.get("uuid")):
            data = body
        if not isinstance(data, dict):
            raise ApiError(f"WebEtu login: unexpected response: {self._error_text(body)}")
        token = str(data.get("token") or "")
        student_uuid = str(data.get("uuid") or "")
        if not token or not student_uuid:
            raise ApiError("WebEtu login failed: " + self._error_text(body))
        self.webetu_token = token
        self.uuid = student_uuid
        LOG.info("WebEtu login successful.")
        return token, student_uuid

    def fetch_annee(self) -> Any:
        body = self.webetu_get("/infos/AnneeAcademiqueEncours", "current academic year")
        data = body.get("data", body) if isinstance(body, dict) else body
        annee_id = data.get("id") if isinstance(data, dict) else data
        if annee_id in (None, ""):
            raise ApiError(f"current academic year: no id in response: {self._error_text(body)}")
        self.annee_id = coerce_id(annee_id)
        LOG.info("Academic year id: %s", self.annee_id)
        return self.annee_id

    def fetch_dia(self) -> Any:
        if not self.uuid or self.annee_id in (None, ""):
            raise ApiError("cannot fetch DIA without student uuid and academic year id")
        body = self.webetu_get(
            f"/infos/bac/{self.uuid}/anneeAcademique/{self.annee_id}/dia",
            "DIA",
        )
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list) and data:
            data = data[0]
        dia_id = data.get("id") if isinstance(data, dict) else data
        if dia_id in (None, ""):
            raise ApiError(f"DIA: no id in response: {self._error_text(body)}")
        self.dia_id = coerce_id(dia_id)
        LOG.info("DIA id: %s", self.dia_id)
        return self.dia_id

    def fetch_wilaya(self) -> Any:
        if self.dia_id in (None, ""):
            raise ApiError("cannot fetch wilaya without a DIA id")
        body = self.webetu_get(f"/infos/wilayaInscription/{self.dia_id}", "wilaya")
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, list) and data:
            data = data[0]
        wilaya_id = data.get("id") if isinstance(data, dict) else data
        if wilaya_id in (None, ""):
            raise ApiError(f"wilaya: no id in response: {self._error_text(body)}")
        self.wilaya = coerce_id(wilaya_id)
        LOG.info("Wilaya id: %s", self.wilaya)
        return self.wilaya

    def fetch_hebergement(self) -> list[dict[str, Any]]:
        if not self.uuid:
            raise ApiError("cannot fetch hebergement history without a student uuid")
        try:
            body = self.webetu_get(
                f"/infos/bac/{self.uuid}/demandesHebregement",
                "hebergement history",
            )
        except ApiError as exc:
            LOG.warning("Could not load hebergement history: %s", exc)
            return []
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            for key in ("demandesHebregement", "items", "data"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            LOG.warning("Hebergement history has an unexpected format; ignoring it.")
            return []
        return [item for item in data if isinstance(item, dict)]

    def pick_residence(self, items: list[dict[str, Any]]) -> Any:
        if self.cfg.residence is not None:
            residence = coerce_id(self.cfg.residence)
            LOG.info("Using configured residence id: %s", residence)
            return residence
        if not items:
            LOG.warning("No hebergement history returned; residence will be omitted from the meal API login.")
            return None
        matching = [item for item in items if str(item.get("idAnneeAcademique")) == str(self.annee_id)]
        if not matching:
            summary = [{"idResidance": item.get("idResidance"), "idAnneeAcademique": item.get("idAnneeAcademique")} for item in items[:10]]
            LOG.warning("No hebergement entry matches academic year %s. Entries: %s", self.annee_id, json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
            LOG.warning("Set WEBETU_RESIDENCE to your residence id if the meal API rejects a login without residence.")
            return None
        residence = coerce_id(matching[0].get("idResidance"))
        if residence is None:
            LOG.warning("Matching hebergement entry has no idResidance.")
        else:
            LOG.info("Residence id: %s", residence)
        return residence

    def meal_auth_payload(self) -> dict[str, Any]:
        payload = {
            "uuid": self.uuid,
            "wilaya": coerce_id(self.wilaya),
            "residence": coerce_id(self.residence),
            "token": self.webetu_token,
        }
        return {key: value for key, value in payload.items() if value is not None and value != ""}

    def login_meal(self) -> str:
        payload = self.meal_auth_payload()
        if payload.get("wilaya") is None:
            raise ApiError("wilaya id is missing; pass --wilaya or set WEBETU_WILAYA")
        headers = {"Content-Type": "application/json"}
        if self.webetu_token:
            headers["authorization"] = self.webetu_token
        if self.cfg.sign:
            headers.update(self._signature_headers(payload))
        _, body = self._request(
            "POST",
            self.cfg.meal_api + "/loginpwebetu",
            label="meal API login",
            headers=headers,
            json_body=payload,
            retries=self.cfg.retries,
        )
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(body, dict) and body.get("token"):
            data = body
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise ApiError("meal API login failed: " + self._error_text(body))
        self.meal_token = str(token)
        LOG.info("Meal API login successful.")
        return self.meal_token

    def meal_get(self, path: str, label: str) -> Any:
        if not self.meal_token:
            raise ApiError("meal API token is missing; login first")
        headers = {"authorization": f"Bearer {self.meal_token}"}
        if self.cfg.sign:
            headers.update(self._signature_headers(None))
        _, body = self._request(
            "GET",
            self.cfg.meal_api + path,
            label=label,
            headers=headers,
            params=self.meal_auth_payload(),
            retries=self.cfg.retries,
        )
        return body

    def fetch_depots(self) -> list[dict[str, Any]]:
        body = self.meal_get("/getdepotres", "restaurant/depot list")
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            depots = data.get("depots")
            if depots is None and isinstance(data.get("data"), list):
                depots = data["data"]
        else:
            depots = data
        if not isinstance(depots, list):
            raise ApiError(f"depot list: unexpected response: {self._error_text(body)}")
        self.depots = [depot for depot in depots if isinstance(depot, dict)]
        LOG.info("Available restaurants/depots: %s", len(self.depots))
        return self.depots

    def get_reservations(self, *, required: bool = False) -> list[dict[str, Any]]:
        try:
            body = self.meal_get("/meal-reservations/student", "current meal reservations")
        except ApiError as exc:
            if required:
                raise
            LOG.warning("Could not load current reservations: %s", exc)
            LOG.warning("Duplicate detection is disabled for this run.")
            return []
        data = body.get("data", body) if isinstance(body, dict) else body
        if isinstance(data, dict):
            for key in ("reservations", "data", "items"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            LOG.warning("Current reservations have an unexpected format; ignoring them.")
            return []
        LOG.debug("Current reservations response: %s", json.dumps(data, ensure_ascii=False, separators=(",", ":"))[:3000])
        return [item for item in data if isinstance(item, dict)]

    def reserve(self, meal_type: int, dates: list[str], depot_id: Any) -> tuple[bool, str]:
        payload = self.meal_auth_payload()
        payload["details"] = [
            json.dumps({"date_reserve": date, "menu_type": meal_type, "idDepot": depot_id}, ensure_ascii=False, separators=(",", ":"))
            for date in dates
        ]
        headers = {
            "Content-Type": "application/json",
            "authorization": f"Bearer {self.meal_token}",
        }
        if self.cfg.sign:
            headers.update(self._signature_headers(payload))
        status, body = self._request(
            "POST",
            self.cfg.meal_api + "/reservemeal",
            label=f"reservation ({MEAL_LABELS[meal_type]})",
            headers=headers,
            json_body=payload,
            retries=0,
        )
        LOG.debug("reservation response (%s): %s", MEAL_LABELS[meal_type], json.dumps(body, ensure_ascii=False, separators=(",", ":"))[:3000])
        return interpret_reservation_response(status, body)

    def authenticate(self) -> None:
        LOG.info("Logging in to WebEtu...")
        self.webetu_login()
        self.fetch_annee()
        if self.cfg.wilaya is not None:
            self.wilaya = coerce_id(self.cfg.wilaya)
            LOG.info("Using configured wilaya id: %s", self.wilaya)
        else:
            self.fetch_dia()
            self.fetch_wilaya()
        if self.cfg.residence is not None:
            self.residence = coerce_id(self.cfg.residence)
            LOG.info("Using configured residence id: %s", self.residence)
        else:
            self.residence = self.pick_residence(self.fetch_hebergement())
        if self.residence is None:
            LOG.warning("Continuing without a residence id; the app omits it too when the hebergement history is unavailable.")
        LOG.info("Logging in to the meal API...")
        self.login_meal()
        LOG.info("Fetching restaurant/depot list...")
        self.fetch_depots()


def interpret_reservation_response(status: int, body: Any) -> tuple[bool, str]:
    data = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(data, list):
        failures = [item for item in data if isinstance(item, dict) and item.get("status") is False]
        if failures:
            messages = []
            for item in failures:
                message = item.get("message") or item.get("error")
                messages.append(str(message) if message else json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            return False, "\n".join(messages)
        return True, f"{len(data)} detail(s) accepted"
    if isinstance(data, dict) and data.get("status") is False:
        message = data.get("message") or data.get("error")
        return False, str(message) if message else json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if status in (200, 201):
        return True, f"HTTP {status}"
    if data == "Reserved" or (isinstance(data, dict) and data.get("status") == "success"):
        return True, "Reserved"
    if isinstance(body, str) and body == "Reserved":
        return True, "Reserved"
    if isinstance(data, dict):
        message = data.get("message") or data.get("error")
        if message:
            return False, str(message)
    return False, f"unexpected response (HTTP {status})"


def reservation_date(reservation: dict[str, Any]) -> str:
    raw = reservation.get("date_reserve") or reservation.get("date") or ""
    return str(raw)[:10]


def meal_type_from_reservation(reservation: dict[str, Any]) -> int | None:
    label = reservation.get("mealtype_fr") or reservation.get("mealLabel") or ""
    normalized = label.casefold().strip()
    if "petit" in normalized:
        return 1
    if "dejeuner" in normalized:
        return 2
    if "diner" in normalized:
        return 3
    return None


def existing_reservation_keys(reservations: list[dict[str, Any]]) -> set[tuple[str, int]]:
    keys: set[tuple[str, int]] = set()
    for reservation in reservations:
        date = reservation_date(reservation)
        meal_type = meal_type_from_reservation(reservation)
        if date and meal_type is not None:
            keys.add((date, meal_type))
    return keys
