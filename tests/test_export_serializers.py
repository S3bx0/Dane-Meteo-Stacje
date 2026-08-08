from dane_meteo_stacje.data import to_noaa_like_payload


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
