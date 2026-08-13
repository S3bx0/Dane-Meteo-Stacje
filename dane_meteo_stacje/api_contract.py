from __future__ import annotations

from typing import Any

from . import __version__


def _json_response(description: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {
            "X-Request-ID": {
                "description": "Correlation identifier for the request",
                "schema": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            }
        },
        "content": {"application/json": {"schema": schema}},
    }


ERROR_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["code", "message", "request_id"],
    "additionalProperties": False,
    "properties": {
        "code": {"type": "string"},
        "message": {"type": "string", "minLength": 1},
        "request_id": {"$ref": "#/components/schemas/RequestId"},
    },
}

HEALTH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok", "status", "request_id"],
    "additionalProperties": True,
    "properties": {
        "ok": {"type": "boolean"},
        "status": {"type": "string"},
        "request_id": {"$ref": "#/components/schemas/RequestId"},
    },
}

OPENAPI_DOCUMENT: dict[str, Any] = {
    "openapi": "3.1.0",
    "info": {
        "title": "Dane Meteo Stacje HTTP API",
        "version": __version__,
        "description": "Local HTTP API used by the Bootstrap station browser.",
    },
    "jsonSchemaDialect": "https://json-schema.org/draft/2020-12/schema",
    "servers": [{"url": "http://127.0.0.1:8765"}],
    "paths": {
        "/": {
            "get": {
                "summary": "Bootstrap GUI",
                "responses": {"200": {"description": "HTML application"}},
            }
        },
        "/health": {
            "get": {
                "summary": "Legacy liveness check",
                "deprecated": True,
                "responses": {"200": _json_response("Process is alive", HEALTH_SCHEMA)},
            }
        },
        "/health/live": {
            "get": {
                "summary": "Process liveness check",
                "responses": {"200": _json_response("Process is alive", HEALTH_SCHEMA)},
            }
        },
        "/health/ready": {
            "get": {
                "summary": "Application readiness check",
                "responses": {
                    "200": _json_response("Application is ready", HEALTH_SCHEMA),
                    "503": _json_response("Application is not ready", HEALTH_SCHEMA),
                },
            }
        },
        "/metrics": {
            "get": {
                "summary": "Prometheus process metrics",
                "responses": {"200": {"description": "Prometheus text exposition"}},
            }
        },
        "/openapi.json": {
            "get": {
                "summary": "Versioned OpenAPI 3.1 contract",
                "responses": {"200": _json_response("OpenAPI document", {"type": "object"})},
            }
        },
        "/api/search": {
            "post": {
                "summary": "Search weather stations",
                "requestBody": {
                    "required": False,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/SearchRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response(
                        "Station results", {"$ref": "#/components/schemas/SearchResponse"}
                    ),
                    "400": _json_response("Invalid request", ERROR_SCHEMA),
                    "403": _json_response("Foreign Origin", ERROR_SCHEMA),
                    "413": _json_response("Payload too large", ERROR_SCHEMA),
                    "415": _json_response("JSON Content-Type required", ERROR_SCHEMA),
                    "502": _json_response("NOAA failure", ERROR_SCHEMA),
                    "503": _json_response("Server busy", ERROR_SCHEMA),
                    "504": _json_response("NOAA deadline exceeded", ERROR_SCHEMA),
                },
            }
        },
        "/api/temperature-capabilities": {
            "post": {
                "summary": "Discover temperature datatypes for a GHCND station",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TemperatureCapabilitiesRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response(
                        "Reported and derived station temperature datatypes",
                        {"$ref": "#/components/schemas/TemperatureCapabilitiesResponse"},
                    ),
                    "400": _json_response("Invalid request", ERROR_SCHEMA),
                    "403": _json_response("Foreign Origin", ERROR_SCHEMA),
                    "413": _json_response("Payload too large", ERROR_SCHEMA),
                    "415": _json_response("JSON Content-Type required", ERROR_SCHEMA),
                    "502": _json_response("NOAA failure", ERROR_SCHEMA),
                    "503": _json_response("Server busy", ERROR_SCHEMA),
                    "504": _json_response("NOAA deadline exceeded", ERROR_SCHEMA),
                },
            }
        },
        "/api/temperatures": {
            "post": {
                "summary": "Download Heatmap, daily, monthly or extended temperatures",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TemperatureRequest"}
                        }
                    },
                },
                "responses": {
                    "200": _json_response(
                        "Requested temperature export",
                        {"$ref": "#/components/schemas/TemperatureResponse"},
                    ),
                    "400": _json_response("Invalid request", ERROR_SCHEMA),
                    "403": _json_response("Foreign Origin", ERROR_SCHEMA),
                    "413": _json_response("Payload too large", ERROR_SCHEMA),
                    "415": _json_response("JSON Content-Type required", ERROR_SCHEMA),
                    "502": _json_response("NOAA failure", ERROR_SCHEMA),
                    "503": _json_response("Server busy", ERROR_SCHEMA),
                    "504": _json_response("NOAA deadline exceeded", ERROR_SCHEMA),
                },
            }
        },
        "/api/export": {
            "post": {
                "summary": "Export station rows as JSON or CSV",
                "responses": {
                    "200": {"description": "Downloadable JSON or CSV document"},
                    "400": _json_response("Invalid request", ERROR_SCHEMA),
                    "403": _json_response("Foreign Origin", ERROR_SCHEMA),
                    "413": _json_response("Payload too large", ERROR_SCHEMA),
                    "415": _json_response("JSON Content-Type required", ERROR_SCHEMA),
                },
            }
        },
    },
    "components": {
        "schemas": {
            "RequestId": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
            "Error": ERROR_SCHEMA,
            "Health": HEALTH_SCHEMA,
            "Station": {
                "type": "object",
                "required": ["station_id", "city", "name", "country"],
                "additionalProperties": True,
                "properties": {
                    "station_id": {"type": "string"},
                    "city": {"type": "string"},
                    "name": {"type": "string"},
                    "country": {"type": "string"},
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "mindate": {"type": "string", "format": "date"},
                    "maxdate": {"type": "string", "format": "date"},
                    "elevation": {"type": "number"},
                    "datacoverage": {"type": "number"},
                },
            },
            "SearchRequest": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "query": {"type": "string"},
                    "country": {"type": "string"},
                    "station_id": {"type": "string"},
                    "sort": {"enum": ["city", "name", "station_id"]},
                    "limit": {"type": "integer", "minimum": 1},
                    "remote_url": {"type": "string", "format": "uri"},
                    "cache_path": {"type": "string"},
                    "cache_ttl": {"type": "integer", "minimum": 0},
                    "refresh": {"type": "boolean"},
                    "allow_sample_fallback": {"type": "boolean"},
                    "stale_if_error": {"type": "boolean"},
                    "max_stale": {"type": "integer", "minimum": 0},
                },
            },
            "SearchResponse": {
                "type": "object",
                "required": ["results", "source", "metadata", "request_id"],
                "additionalProperties": False,
                "properties": {
                    "results": {
                        "type": "array",
                        "items": {"$ref": "#/components/schemas/Station"},
                    },
                    "source": {"type": "string"},
                    "metadata": {"type": "object"},
                    "request_id": {"$ref": "#/components/schemas/RequestId"},
                },
            },
            "TemperatureRequest": {
                "type": "object",
                "required": ["station_id", "start_year", "end_year"],
                "additionalProperties": False,
                "properties": {
                    "station_id": {"type": "string", "minLength": 1},
                    "start_year": {"type": "integer", "minimum": 1763},
                    "end_year": {"type": "integer", "minimum": 1763},
                    "mode": {"enum": ["heatmap", "daily", "monthly", "extended"], "default": "heatmap"},
                },
            },
            "TemperatureCapabilitiesRequest": {
                "type": "object",
                "required": ["station_id"],
                "additionalProperties": False,
                "properties": {"station_id": {"type": "string", "minLength": 1}},
            },
            "TemperatureCapabilities": {
                "type": "object",
                "required": [
                    "station_id",
                    "dataset_id",
                    "available_datatypes",
                    "core_temperature_datatypes",
                    "derived_datatypes",
                    "export_modes",
                    "temperature_methods",
                ],
                "additionalProperties": True,
                "properties": {
                    "station_id": {"type": "string"},
                    "dataset_id": {"const": "GHCND"},
                    "available_datatypes": {"type": "array", "items": {"type": "string"}},
                    "core_temperature_datatypes": {"type": "array", "items": {"type": "string"}},
                    "derived_datatypes": {"type": "object", "additionalProperties": {"type": "boolean"}},
                    "export_modes": {"type": "object", "additionalProperties": {"type": "boolean"}},
                    "temperature_methods": {"type": "object"},
                },
            },
            "TemperatureCapabilitiesResponse": {
                "type": "object",
                "required": ["data", "request_id"],
                "additionalProperties": False,
                "properties": {
                    "data": {"$ref": "#/components/schemas/TemperatureCapabilities"},
                    "request_id": {"$ref": "#/components/schemas/RequestId"},
                },
            },
            "TemperatureMatrix": {
                "type": "object",
                "required": [
                    "station_id",
                    "years",
                    "months",
                    "temperatures",
                    "final_missing_years",
                    "missing_data_report",
                    "token_usage",
                    "adaptive_history",
                ],
                "additionalProperties": False,
                "properties": {
                    "station_id": {"type": "string"},
                    "years": {"type": "array", "items": {"type": "integer"}},
                    "months": {"type": "array", "items": {"type": "string"}},
                    "temperatures": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": ["number", "null"]},
                            "minItems": 12,
                            "maxItems": 12,
                        },
                    },
                    "final_missing_years": {"type": "array", "items": {"type": "integer"}},
                    "missing_data_report": {"type": "object"},
                    "token_usage": {"type": "object"},
                    "adaptive_history": {"type": "array", "items": {"type": "object"}},
                },
            },
            "TemperatureResponse": {
                "type": "object",
                "required": ["data", "request_id"],
                "additionalProperties": False,
                "properties": {
                    "data": {
                        "anyOf": [
                            {"$ref": "#/components/schemas/TemperatureMatrix"},
                            {"type": "object", "additionalProperties": True},
                        ]
                    },
                    "request_id": {"$ref": "#/components/schemas/RequestId"},
                },
            },
        }
    },
}
