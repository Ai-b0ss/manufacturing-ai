"""A product-level acceptance test: one genuine linked BOM/PDF/STEP report.

Runs the new unified app in-process with signed Keycloak tokens and the
existing read-only public CAD demo; does not modify PostgreSQL or restart P13.
"""
from __future__ import annotations

import os
from copy import deepcopy
from unittest.mock import patch
from fastapi.testclient import TestClient

from core06.engineering_kit_report import InvalidEngineeringKit,build_engineering_kit_report


def main():
    from tools.p3_gateway_smoke import mint
    import p2_gateway.app as p2module
    import unified_app
    engineer={"Authorization":"Bearer "+mint("engineer")}
    finance={"Authorization":"Bearer "+mint("finance")}
    with (patch.object(p2module,"APP_ORIGIN","http://localhost:19139"),
          patch.object(unified_app,"APP_ORIGIN","http://localhost:19139"),
          TestClient(unified_app.app,base_url="http://localhost:19139") as client):
        page=client.get("/")
        assert page.status_code==200
        assert 'id="kit-report-generate"' in page.text
        script=client.get("/app.js")
        assert script.status_code==200
        assert "generateKitReport" in script.text and "saveKitReport" in script.text
        response=client.get(
            "/unified/api/engineering/kit-report?q=focuser_drawtube",
            headers=engineer,
        )
        assert response.status_code==200,response.text[:250]
        report=response.json()
        assert report["part"]=="focuser_drawtube"
        assert report["schema"]=="factory_engineering_kit_report_v1"
        assert report["state"]=="ENGINEERING_REVIEW_REQUIRED"
        assert report["manufacturing_authorization"] is False
        assert [x["role"] for x in report["sources"]]==["BOM","DRAWING","MODEL"]
        assert len({x["sha256"] for x in report["sources"]})==3
        assert report["drawing_annotations"]>0
        assert report["cad_features"]>0
        assert all(x["download_path"].startswith("/api/engineering/file?id=")
                   for x in report["sources"])
        assert not any(x.get("result")=="GEOMETRY_CERTIFIED" for x in report["dimension_checks"])
        assert client.get(
            "/unified/api/engineering/kit-report?q=focuser_drawtube",
            headers=finance,
        ).status_code==403
        assert client.get(
            "/unified/api/engineering/kit-report?q=../../wrong",
            headers=engineer,
        ).status_code==422
    original={
        "source_match":{"state":"BOM_DRAWING_STEP_LINKED","part":"piece",
           "part_key":"piece","sources":[
            {"role":"BOM","id":"a"*24,"name":"bom.csv","sha256":"a"*64},
            {"role":"DRAWING","id":"b"*24,"name":"x.pdf","sha256":"b"*64},
            {"role":"MODEL","id":"c"*24,"name":"x.step","sha256":"c"*64},
           ]}
    }
    geometry={"part":"piece","drawing":{"id":"b"*24,"sha256":"b"*64},
       "model":{"id":"c"*24,"sha256":"c"*64},
       "drawing_annotations":[],"features":[]}
    result=build_engineering_kit_report(original,geometry)
    assert not result["manufacturing_authorization"]
    changed=deepcopy(geometry);changed["model"]["sha256"]="d"*64
    try:
        build_engineering_kit_report(original,changed)
        raise AssertionError("mismatched STEP hash accepted")
    except InvalidEngineeringKit:
        pass
    print("PASS real Keycloak+PostgreSQL+P13 CAD kit: report, three SHA-bound sources, no false approval, finance denied, changed STEP rejected")


if __name__=="__main__":
    main()
