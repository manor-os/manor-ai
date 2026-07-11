"""Weather tool — fetch current weather and forecast for a location.

Provider priority:
  1. WEATHER_API_KEY set → use WEATHER_API_PROVIDER (weatherapi | openweathermap)
  2. No key → wttr.in (free, no API key needed)
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

WEATHER_SEARCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "weather_search",
        "description": (
            "Get current weather and forecast for a location. "
            "Returns temperature, conditions, humidity, wind, and multi-day forecast."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City name, address, or coordinates (e.g. 'New York', 'Tokyo', '40.7,-74.0')",
                },
                "units": {
                    "type": "string",
                    "enum": ["metric", "imperial"],
                    "description": "Temperature units: metric (Celsius) or imperial (Fahrenheit). Default: metric.",
                },
            },
            "required": ["location"],
        },
    },
}


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _weather_search(entity_id: str, **kwargs: Any) -> str:
    location = (kwargs.get("location") or "").strip()
    if not location:
        return json.dumps({"error": "location is required"})

    units = kwargs.get("units", "metric")
    api_key = os.getenv("WEATHER_API_KEY", os.getenv("OPENWEATHERMAP_API_KEY", ""))
    provider = os.getenv("WEATHER_API_PROVIDER", "weatherapi").strip().lower()

    try:
        if api_key:
            if provider == "openweathermap":
                return await _openweathermap(api_key, location, units)
            else:
                return await _weatherapi(api_key, location, units)
        else:
            # Free fallback: wttr.in (no API key needed)
            return await _wttr_in(location, units)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            logger.warning("Weather API 401 — falling back to wttr.in")
            return await _wttr_in(location, units)
        logger.error("Weather API error %d for %s", e.response.status_code, location)
        return json.dumps({"error": f"Weather API error: {e.response.status_code}"})
    except Exception as e:
        logger.error("Weather search failed: %s", e)
        return json.dumps({"error": f"Weather search failed: {e}"})


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

async def _wttr_in(location: str, units: str) -> str:
    """Free weather via wttr.in — no API key needed."""
    # wttr.in JSON format: ?format=j1
    url_location = location.replace(" ", "+")
    params = "m" if units == "metric" else "u"

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"https://wttr.in/{url_location}?format=j1&{params}",
            headers={"User-Agent": "Manor-AI/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()

    current = data.get("current_condition", [{}])[0]
    area = data.get("nearest_area", [{}])[0]

    if units == "metric":
        temp = current.get("temp_C", "")
        feels = current.get("FeelsLikeC", "")
        wind = current.get("windspeedKmph", "")
        wind_unit = "km/h"
    else:
        temp = current.get("temp_F", "")
        feels = current.get("FeelsLikeF", "")
        wind = current.get("windspeedMiles", "")
        wind_unit = "mph"

    # Parse forecast
    forecast = []
    for day in data.get("weather", [])[:3]:
        if units == "metric":
            high, low = day.get("maxtempC", ""), day.get("mintempC", "")
        else:
            high, low = day.get("maxtempF", ""), day.get("mintempF", "")
        hourly = day.get("hourly", [{}])
        mid = hourly[len(hourly) // 2] if hourly else {}
        forecast.append({
            "date": day.get("date", ""),
            "high": _safe_num(high),
            "low": _safe_num(low),
            "conditions": mid.get("weatherDesc", [{}])[0].get("value", ""),
            "chance_of_rain": mid.get("chanceofrain", ""),
        })

    area_name = area.get("areaName", [{}])[0].get("value", location)
    country = area.get("country", [{}])[0].get("value", "")

    result = {
        "location": {"name": area_name, "country": country},
        "current": {
            "temperature": _safe_num(temp),
            "feels_like": _safe_num(feels),
            "humidity": _safe_num(current.get("humidity", "")),
            "conditions": current.get("weatherDesc", [{}])[0].get("value", ""),
            "wind_speed": _safe_num(wind),
            "wind_unit": wind_unit,
        },
        "forecast": forecast,
        "units": units,
    }
    return json.dumps(result)


async def _weatherapi(api_key: str, location: str, units: str) -> str:
    """WeatherAPI.com — paid provider with generous free tier."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            "https://api.weatherapi.com/v1/forecast.json",
            params={"key": api_key, "q": location, "days": 3},
        )
        resp.raise_for_status()
        data = resp.json()

    c = data["current"]
    loc = data["location"]

    if units == "metric":
        temp, feels = c["temp_c"], c["feelslike_c"]
        wind, wind_unit = c["wind_kph"], "km/h"
    else:
        temp, feels = c["temp_f"], c["feelslike_f"]
        wind, wind_unit = c["wind_mph"], "mph"

    forecast = []
    for day in data.get("forecast", {}).get("forecastday", []):
        d = day["day"]
        if units == "metric":
            high, low = d["maxtemp_c"], d["mintemp_c"]
        else:
            high, low = d["maxtemp_f"], d["mintemp_f"]
        forecast.append({
            "date": day["date"],
            "high": high,
            "low": low,
            "conditions": d["condition"]["text"],
            "chance_of_rain": d.get("daily_chance_of_rain", ""),
        })

    result = {
        "location": {"name": loc["name"], "country": loc["country"]},
        "current": {
            "temperature": temp,
            "feels_like": feels,
            "humidity": c["humidity"],
            "conditions": c["condition"]["text"],
            "wind_speed": wind,
            "wind_unit": wind_unit,
        },
        "forecast": forecast,
        "units": units,
    }
    return json.dumps(result)


async def _openweathermap(api_key: str, location: str, units: str) -> str:
    """OpenWeatherMap — classic weather API."""
    base = "https://api.openweathermap.org/data/2.5"

    async with httpx.AsyncClient(timeout=15) as client:
        # Current weather
        resp = await client.get(
            f"{base}/weather",
            params={"q": location, "appid": api_key, "units": units},
        )
        resp.raise_for_status()
        cur = resp.json()

        # 5-day forecast (3-hour intervals)
        resp2 = await client.get(
            f"{base}/forecast",
            params={"q": location, "appid": api_key, "units": units, "cnt": 24},
        )
        resp2.raise_for_status()
        fc = resp2.json()

    wind_unit = "m/s" if units == "metric" else "mph"

    # Aggregate forecast by day
    forecast = []
    for item in fc.get("list", [])[::8][:3]:  # ~daily
        forecast.append({
            "datetime": item.get("dt_txt", ""),
            "temperature": item["main"]["temp"],
            "conditions": item["weather"][0]["description"],
            "humidity": item["main"]["humidity"],
        })

    result = {
        "location": {
            "name": cur.get("name", location),
            "country": cur.get("sys", {}).get("country", ""),
        },
        "current": {
            "temperature": cur["main"]["temp"],
            "feels_like": cur["main"]["feels_like"],
            "humidity": cur["main"]["humidity"],
            "conditions": cur["weather"][0]["description"],
            "wind_speed": cur["wind"]["speed"],
            "wind_unit": wind_unit,
        },
        "forecast": forecast,
        "units": units,
    }
    return json.dumps(result)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_num(val: Any) -> float | int | None:
    """Convert string numbers to int/float, return None if invalid."""
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return int(f) if f == int(f) else f
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def get_tools() -> list[tuple[dict, callable]]:
    return [
        (WEATHER_SEARCH_SCHEMA, _weather_search),
    ]
