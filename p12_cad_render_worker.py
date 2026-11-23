"""P12 offline OpenCascade / VTK CAD evidence renderer.

Executes only as a bounded subprocess for SHA-verified public STEP bytes.
A selected feature is identified using exactly the P10 analytic-face grouping
algorithm (kind, radius, axis, axis origin or absolute plane normal), not by
untrusted input topology indices.
"""
from __future__ import annotations
import base64
import hashlib
import json
import math
from pathlib import Path
import sys

MAX_STEP_BYTES=8_000_000
MAX_FACES=25000
MAX_TRIANGLES=350000
MAX_POINTS=900000
ALLOWED_VIEWS=("iso","front","side","top")
SUPPORTED_FEATURES=("CYLINDER_RADIUS","PLANE_DIRECTION")
PNG_LIMIT=3_000_000
TARGET_WIDTH=1000
TARGET_HEIGHT=660

def feature_key(surface):
    from OCP.GeomAbs import GeomAbs_Cylinder,GeomAbs_Plane
    typ=surface.GetType()
    if typ==GeomAbs_Cylinder:
        cyl=surface.Cylinder()
        direction=cyl.Axis().Direction()
        center=cyl.Location()
        radius=round(float(cyl.Radius()),5)
        xyz=[round(float(getattr(center,axis)()),3) for axis in ("X","Y","Z")]
        vec=[round(float(getattr(direction,axis)()),5) for axis in ("X","Y","Z")]
        if radius>0 and math.isfinite(radius):
            canonical=tuple(vec) if next((v for v in vec if abs(v)>1e-6),1)>0 else tuple(-v for v in vec)
            return ("cylinder",radius,canonical,tuple(xyz))
    if typ==GeomAbs_Plane:
        normal=surface.Plane().Axis().Direction()
        components=tuple(round(abs(float(getattr(normal,a)())),6) for a in ("X","Y","Z"))
        if all(math.isfinite(x) for x in components):
            return ("plane",components)
    return None

def build_polydata(faces):
    import vtk
    poly=vtk.vtkPolyData()
    vtk_points=vtk.vtkPoints()
    vtk_points.SetDataTypeToFloat()
    polys=vtk.vtkCellArray()
    for triangle in faces:
        for vertex in triangle:
            vtk_points.InsertNextPoint(*vertex)
        index=vtk_points.GetNumberOfPoints()-3
        polys.InsertNextCell(3)
        for n in (index,index+1,index+2):
            polys.InsertCellPoint(n)
    poly.SetPoints(vtk_points)
    poly.SetPolys(polys)
    return poly

def inspect_and_render(filename,feature_id,view="iso"):
    if not isinstance(feature_id,str) or len(feature_id)!=24 or not all(x in "0123456789abcdef" for x in feature_id):
        raise ValueError("FEATURE_ID_INVALID")
    if view not in ALLOWED_VIEWS:
        raise ValueError("VIEW_INVALID")
    file=Path(filename)
    if not file.is_file() or file.stat().st_size>MAX_STEP_BYTES:
        raise ValueError("STEP_MISSING_OR_TOO_LARGE")
    data=file.read_bytes()
    if not data.lstrip().startswith(b"ISO-10303-21;"):
        raise ValueError("NOT_STEP")
    sha=hashlib.sha256(data).hexdigest()

    from OCP.STEPControl import STEPControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TopoDS import TopoDS
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRep import BRep_Tool
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.collections import Sequence_TCollection_AsciiString
    import vtk

    reader=STEPControl_Reader()
    if reader.ReadFile(str(file))!=IFSelect_RetDone:
        raise ValueError("STEP_READ_FAILED")
    lengths=Sequence_TCollection_AsciiString()
    angles=Sequence_TCollection_AsciiString()
    solids=Sequence_TCollection_AsciiString()
    reader.FileUnits(lengths,angles,solids)
    length_units=[lengths.Value(i).ToCString() for i in range(1,lengths.Length()+1)]
    reader.SetSystemLengthUnit(1.0)
    if reader.TransferRoots()<1:raise ValueError("STEP_TRANSFER_FAILED")
    shape=reader.OneShape()
    if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
        raise ValueError("STEP_TOPOLOGY_INVALID")
    # Only original files explicitly declaring mm may be visualized under a mm label.
    if length_units!=["millimetre"]:
        raise ValueError("STEP_UNITS_UNKNOWN")
    mesher=BRepMesh_IncrementalMesh(shape,0.65,False,0.5,False)
    mesher.Perform()
    if not mesher.IsDone():raise ValueError("STEP_MESH_FAILED")
    total_tri=0
    total_points=0
    face_index=0
    selected_face_indices=[]
    highlighted=[]
    neutral=[]
    exp=TopExp_Explorer(shape,TopAbs_FACE)
    while exp.More():
        face_index+=1
        if face_index>MAX_FACES:raise ValueError("FACE_COUNT_LIMIT")
        face=TopoDS.Face(exp.Current())
        surface=BRepAdaptor_Surface(face)
        key=feature_key(surface)
        wanted=False
        if key is not None:
            payload=json.dumps({"sha256":sha,"key":key},sort_keys=True,separators=(',',':'))
            current_id=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
            wanted=(current_id==feature_id)
        loc=TopLoc_Location()
        mesh=BRep_Tool.Triangulation_s(face,loc)
        if mesh is None:raise ValueError("MESH_FACE_MISSING")
        nodes=mesh.NbNodes()
        tris=mesh.NbTriangles()
        total_tri+=tris
        total_points+=nodes
        if total_tri>MAX_TRIANGLES or total_points>MAX_POINTS:
            raise ValueError("MESH_SIZE_LIMIT")
        if wanted:selected_face_indices.append(face_index)
        points=[]
        transform=loc.Transformation()
        for i in range(1,nodes+1):
            xyz=mesh.Node(i).Transformed(transform)
            point=(float(xyz.X()),float(xyz.Y()),float(xyz.Z()))
            if not all(math.isfinite(v) and abs(v)<1e7 for v in point):
                raise ValueError("INVALID_CAD_COORDINATES")
            points.append(point)
        destination=highlighted if wanted else neutral
        for i in range(1,tris+1):
            tri=mesh.Triangle(i)
            a,b,c=tri.Get()
            if any(v<1 or v>nodes for v in (a,b,c)):
                raise ValueError("INVALID_CAD_TRIANGLE")
            destination.append((points[a-1],points[b-1],points[c-1]))
        exp.Next()

    if not selected_face_indices or not highlighted or not neutral:
        raise ValueError("FEATURE_NOT_MESHABLE")
    # Render both groups with depth testing and consistent camera, so
    # highlighted faces are not simply painted over occluding geometry.
    render=vtk.vtkRenderer()
    render.SetBackground(0.965,0.975,0.985)
    window=vtk.vtkRenderWindow()
    window.SetOffScreenRendering(True)
    window.SetMultiSamples(0)
    window.SetSize(TARGET_WIDTH,TARGET_HEIGHT)
    window.AddRenderer(render)

    def actor(triangles,rgb,edges):
        poly=build_polydata(triangles)
        mapper=vtk.vtkPolyDataMapper()
        mapper.SetInputData(poly)
        actor=vtk.vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(*rgb)
        actor.GetProperty().SetInterpolationToFlat()
        actor.GetProperty().SetAmbient(0.24)
        actor.GetProperty().SetDiffuse(0.76)
        if edges and len(triangles)<12000:
            actor.GetProperty().EdgeVisibilityOn()
            actor.GetProperty().SetEdgeColor(0.68,0.36,0.05)
            actor.GetProperty().SetLineWidth(1.0)
        render.AddActor(actor)
        return actor

    actor(neutral,(0.69,0.76,0.82),False)
    actor(highlighted,(0.95,0.34,0.07),True)
    render.ResetCamera()
    center=render.ComputeVisiblePropBounds()
    cx=(center[0]+center[1])/2
    cy=(center[2]+center[3])/2
    cz=(center[4]+center[5])/2
    scale=max(center[1]-center[0],center[3]-center[2],center[5]-center[4],1)
    view_vectors={
        "iso":(1.6,-1.85,1.35),
        "front":(0,-1,0.12),
        "side":(1,0,0.12),
        "top":(0,0,1)
    }
    vx,vy,vz=view_vectors[view]
    cam=render.GetActiveCamera()
    cam.SetFocalPoint(cx,cy,cz)
    cam.SetPosition(cx+3*scale*vx,cy+3*scale*vy,cz+3*scale*vz)
    cam.SetViewUp(0,0,1 if view!="top" else 0)
    if view=="top":cam.SetViewUp(0,1,0)
    cam.ParallelProjectionOn()
    render.ResetCamera()
    render.ResetCameraClippingRange()
    window.Render()
    image=vtk.vtkWindowToImageFilter()
    image.SetInput(window)
    image.SetInputBufferTypeToRGB()
    image.ReadFrontBufferOff()
    image.Update()
    writer=vtk.vtkPNGWriter()
    writer.WriteToMemoryOn()
    writer.SetInputConnection(image.GetOutputPort())
    writer.Write()
    png=bytes(memoryview(writer.GetResult()))
    if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png)>PNG_LIMIT:
        raise ValueError("PNG_INVALID_OR_TOO_LARGE")
    window.Finalize()
    return {
        "state":"CAD_FEATURE_RENDERED",
        "source_sha256":sha,
        "selected_feature_id":feature_id,
        "selected_face_indices":selected_face_indices,
        "selected_face_count":len(selected_face_indices),
        "all_face_count":face_index,
        "triangle_count":total_tri,
        "selected_triangle_count":len(highlighted),
        "view":view,
        "width":TARGET_WIDTH,
        "height":TARGET_HEIGHT,
        "png_base64":base64.b64encode(png).decode("ascii"),
        "manufacturing_authorization":False,
        "warning":"Rendering highlights real selected analytic faces; PDF callout-to-face mapping is not proved."
    }

if __name__=="__main__":
    try:
        if len(sys.argv)!=4:raise ValueError("EXPECTED_STEP_FEATURE_VIEW")
        result=inspect_and_render(sys.argv[1],sys.argv[2],sys.argv[3])
        print("P8_RESULT_JSON:"+json.dumps(result,separators=(",",":"),ensure_ascii=True))
    except Exception as exc:
        print("P8_RESULT_JSON:"+json.dumps({"error":type(exc).__name__,"message":str(exc)[:90]}))
        sys.exit(2)
