"""Acceptance for first USER-supplied private CAD package.

Actual public test files are sent as arbitrary upload bytes through the new
protected route; the production corpus is not modified.
"""
from __future__ import annotations

import json,os
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient

from core06.private_cad_bundle import MAGIC

ROOT=Path(__file__).resolve().parents[1]
_SIBLING_SOURCES=ROOT.parent/"ManufacturingAI-Design-Preview"/".preview"/"public_engineering_corpus_20261117"/"ArcBlue_HMT_Lite"
_CANONICAL_SOURCES=Path(r"C:\Users\user\Documents\ManufacturingAI-Design-Preview\.preview\public_engineering_corpus_20261117\ArcBlue_HMT_Lite")
SOURCES=Path(os.environ["FACTORY_PUBLIC_CAD_DIR"]) if os.environ.get("FACTORY_PUBLIC_CAD_DIR") else (_SIBLING_SOURCES if _SIBLING_SOURCES.is_dir() else _CANONICAL_SOURCES)


def package(part="focuser_drawtube",replace=None,step_name=None):
    files=[
        ("bom","materials_bom.csv"),("drawing","focuser_drawtube.pdf"),
        ("model","focuser_drawtube.STEP")
    ]
    manifest={"part":part,"files":[]}
    payload=[]
    for role,name in files:
        body=(SOURCES/name).read_bytes()
        if replace and role in replace:
            body=replace[role]
        declared_name=step_name if role=="model" and step_name else name
        manifest["files"].append({"role":role,"name":declared_name,"size":len(body)})
        payload.append(body)
    header=json.dumps(manifest,ensure_ascii=False).encode()
    return MAGIC+len(header).to_bytes(4,"big")+header+b"".join(payload)


def main():
    assert SOURCES.exists()
    from tools.p3_gateway_smoke import mint
    import p2_gateway.app as p2module
    import unified_app
    engineer={"Authorization":"Bearer "+mint("engineer"),
              "Content-Type":"application/octet-stream"}
    finance={"Authorization":"Bearer "+mint("finance"),
             "Content-Type":"application/octet-stream"}
    with (patch.object(p2module,"APP_ORIGIN","http://localhost:19139"),
          patch.object(unified_app,"APP_ORIGIN","http://localhost:19139"),
          TestClient(unified_app.app,base_url="http://localhost:19139") as client):
        homepage=client.get("/")
        assert homepage.status_code==200
        assert 'id="private-kit-form"' in homepage.text
        assert 'id="private-kit-pdf"' in homepage.text
        assert 'id="private-kit-step"' in homepage.text
        payload=package()
        response=client.post("/unified/api/engineering/bundle-check",
                             content=payload,headers=engineer)
        assert response.status_code==200,response.text[:250]
        report=response.json()
        assert report["schema"]=="factory_private_cad_bundle_v1"
        assert report["part"]=="focuser_drawtube"
        assert report["manufacturing_authorization"] is False
        assert report["source_persistence"]=="NONE"
        assert report["bom"]["material"]=="6061-T6 Aluminum Alloy"
        assert report["model"]["face_count"]>30
        assert report["drawing"]["dimension_count"]>10
        assert len(report["sources"])==3
        assert any(d["label"]=="R41.5" and d["numeric_candidates"]
                   for d in report["dimension_checks"])
        assert all(d["geometry_mapping"]=="NOT_VERIFIED"
                   for d in report["dimension_checks"])
        assert "no-store" in response.headers["cache-control"]
        print("PASS live CAD files PDF+STEP+CSV analyzed in ephemeral unshared workspace")
        short_suffix=client.post("/unified/api/engineering/bundle-check",
                                 content=package(step_name="focuser_drawtube.stp"),
                                 headers=engineer)
        assert short_suffix.status_code==200
        assert short_suffix.json()["sources"][2]["name"]=="focuser_drawtube.stp"
        print("PASS standard STEP .stp extension is accepted")


        denied=client.post("/unified/api/engineering/bundle-check",
                           content=payload,headers=finance)
        assert denied.status_code==403,denied.text[:150]
        print("PASS finance role cannot process restricted engineering package")

        absent=client.post("/unified/api/engineering/bundle-check",
                           content=package(part="no_such_part"),headers=engineer)
        assert absent.status_code==422 and "PART_NOT_FOUND_IN_BOM" in absent.text
        print("PASS unrelated part designation not claimed as valid BOM link")

        corrupted=client.post("/unified/api/engineering/bundle-check",
                              content=package(replace={"model":b"NOT A STEP FILE"}),
                              headers=engineer)
        assert corrupted.status_code==422 and "STEP_SIGNATURE_INVALID" in corrupted.text
        print("PASS corrupted STEP does not produce fake CAD evidence")

        malformed=client.post("/unified/api/engineering/bundle-check",
                              content=payload+b"JUNK",headers=engineer)
        assert malformed.status_code==422 and "PACKAGE_TRAILING_BYTES" in malformed.text
        print("PASS package framing rejects hidden extra bytes")

    # Test workers clean up their scratch directories.
    import tempfile
    leftovers=list(Path(tempfile.gettempdir()).glob("czkit-private-*"))
    assert not leftovers, "CAD temporary files remained: "+str(leftovers[:2])
    print("PASS all input files removed from temporary workspace")


if __name__=="__main__":
    main()
