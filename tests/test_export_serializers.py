import csv
import json

from dane_meteo_stacje.data import export_stations, to_noaa_like_payload


def test_to_noaa_like_payload_has_expected_shape():
    payload = to_noaa_like_payload(
        [
            {
                "station_id": "PLM00012295",
                "city": "Bialystok",
                "name": "Bialystok",
                "country": "Poland",
                "source": "NOAA",
            }
        ]
    )

    assert payload["metadata"]["resultset"]["count"] == 1
    assert payload["results"][0]["id"] == "PLM00012295"
    assert payload["results"][0]["city"] == "Bialystok"
    assert payload["results"][0]["source"] == "NOAA"


def test_to_noaa_like_payload_handles_empty_input():
    payload = to_noaa_like_payload([])

    assert payload == {"metadata": {"resultset": {"count": 0}}, "results": []}


def test_export_stations_writes_pretty_noaa_json_and_csv(tmp_path):
    stations = [
        {
            "station_id": "PLM00012295",
            "city": "Bialystok",
            "name": "Bialystok",
            "country": "Poland",
        }
    ]
    json_path = tmp_path / "noaa.json"
    csv_path = tmp_path / "noaa.csv"

    export_stations(
        stations,
        output_json=json_path,
        output_csv=csv_path,
        pretty=True,
        noaa_like=True,
    )

    json_text = json_path.read_text(encoding="utf-8")
    assert "\n  \"metadata\"" in json_text
    assert json.loads(json_text)["results"][0]["id"] == "PLM00012295"
    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows == [
        {
            "id": "PLM00012295",
            "name": "Bialystok",
            "city": "Bialystok",
            "country": "Poland",
            "source": "local",
        }
    ]
