"""Isolated temporary fixture server for attachment browser integration checks."""
import json
import sys
import tempfile
import zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, initialize_admin
from werkzeug.serving import make_server

with tempfile.TemporaryDirectory(prefix='notice-browser-test-') as directory:
    folder = Path(directory)
    with zipfile.ZipFile(folder / '课程说明.docx', 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', '<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>课程附件预览测试：请按时提交实验报告。</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>课程</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>计算机组成原理</w:t></w:r></w:p></w:tc></w:tr></w:tbl><w:sectPr/></w:body></w:document>')
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>', b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    stream = b'BT /F1 16 Tf 30 340 Td (Class notice attachment preview) Tj ET'
    objects.append(b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream')
    data=bytearray(b'%PDF-1.4\n'); offsets=[0]
    for index, obj in enumerate(objects,1):
        offsets.append(len(data)); data.extend(str(index).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    xref=len(data);data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]: data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n')
    (folder/'实验要求.pdf').write_bytes(data)
    app=create_app({'TESTING':True,'DATABASE':str(folder/'test.sqlite3'),'SECRET_KEY':'isolated-browser-integration-test-secret'})
    initialize_admin(app,'browser-admin','browser-test-password')
    server=make_server('127.0.0.1',0,app,threaded=True)
    print(json.dumps({'port':server.server_port,'fixtures':str(folder)}),flush=True)
    try: server.serve_forever()
    finally: server.server_close()
