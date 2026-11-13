"""Adversarial payloads prove privacy checks cannot be bypassed by nesting/metadata."""
import base64
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib
import marshal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
import privacy_gate as p


def archive(files):
    b=io.BytesIO()
    with zipfile.ZipFile(b,"w",zipfile.ZIP_DEFLATED) as z:
        for name,data in files.items(): z.writestr(name,data)
    return b.getvalue()


def png_chunk(kind,data):
    return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data))


class PrivacyGateTests(unittest.TestCase):
    def scan(self,data,name="test.zip"):
        s=p.Scanner();s.visit(name,data);return s.result()

    def test_clean_nested_archive(self):
        r=self.scan(archive({'a.zip':archive({'guide.txt':b'public data'})}))
        self.assertTrue(r['passed']);self.assertEqual(r['counts']['zip_archives'],2)

    def test_nested_personal_path(self):
        r=self.scan(archive({'a.zip':archive({'record.txt':b'C:\\Users\\PrivatePerson\\notes.txt'})}))
        self.assertFalse(r['passed']);self.assertIn('!a.zip!record.txt',r['findings'][0]['file'])

    def test_utf16_private_path(self):
        for enc in ('utf-16-le','utf-16-be'):
            r=self.scan(archive({'blob.lib':'C:\\Users\\PrivatePerson\\notes'.encode(enc)}))
            self.assertFalse(r['passed'])

    def test_json_escaped_path(self):
        r=self.scan(archive({'data.json':r'{"location":"C:\u005cUsers\u005cPrivatePerson\u005cnotes"}'}))
        self.assertFalse(r['passed'])

    def test_private_record_even_without_pattern(self):
        self.assertFalse(self.scan(archive({'复核记录/run.json':'{}'}))['passed'])

    def test_private_page_link(self):
        self.assertFalse(self.scan(archive({'index.html':'https://chatgpt.com/space/page_private'}))['passed'])

    def test_credential(self):
        self.assertFalse(self.scan(archive({'data.txt':'ghp_'+'A'*36}))['passed'])

    def test_separate_locale_strings_are_not_credentials(self):
        self.assertTrue(self.scan(b'sk-SK\0'+b'ru-RU\0'*20,'library.lib')['passed'])

    def test_private_account(self):
        self.assertFalse(self.scan(archive({'data.json':'{"player_uid":"123456789"}'}))['passed'])

    def test_png_exif_and_inline_image(self):
        image=b'\x89PNG\r\n\x1a\n'+png_chunk(b'eXIf',b'author=PrivatePerson')+png_chunk(b'IEND',b'')
        for blob,name in [(image,'image.png'),(('data:image/png;base64,'+base64.b64encode(image).decode()).encode(),'page.html')]:
            self.assertFalse(self.scan(blob,name)['passed'])

    def test_zip_metadata(self):
        b=io.BytesIO()
        with zipfile.ZipFile(b,'w') as z:
            z.comment=b'PrivatePerson';z.writestr('ok.txt','public')
        self.assertFalse(self.scan(b.getvalue())['passed'])

    def test_unsupported_archive_fails_closed(self):
        self.assertFalse(self.scan(archive({'hidden.7z':b'opaque'}))['passed'])

    def test_traversal_and_duplicate_members(self):
        self.assertFalse(self.scan(archive({'../record.txt':b'bad'}))['passed'])

    def test_embedded_pyz(self):
        payload=zlib.compress(b'C:\\Users\\PrivatePerson\\module.py')
        toc=marshal.dumps({'module':(0,12,len(payload))})
        pyz=b'PYZ\0'+b'\0'*4+struct.pack('!I',12+len(payload))+payload+toc
        self.assertFalse(self.scan(pyz,'PYZ.pyz')['passed'])

    def test_embedded_pyinstaller(self):
        raw=b'C:\\Users\\PrivatePerson\\module.py';payload=zlib.compress(raw)
        label=b'module\0';entry=struct.pack('!IIIIBc',18+len(label),0,len(payload),len(raw),1,b's')+label
        length=len(payload)+len(entry)+88
        blob=payload+entry+struct.pack('!8sIIII64s',p.MEI,length,len(payload),len(entry),313,b'python313.dll')
        s=p.Scanner();s.carchive('engine.exe',blob,0)
        self.assertFalse(s.result()['passed'])

    def test_unrecognized_debug_path(self):
        # Real PE parser is exercised separately against the shipped payload.
        with patch.object(p,'pe_debug',return_value=[{'path':'C:/Users/PrivatePerson/source/app.pdb'}]):
            self.assertFalse(self.scan(b'MZ'+b'\0'*100,'app.dll')['passed'])

    def test_video_author_metadata(self):
        metadata={'format':{'tags':{'artist':'PrivatePerson'}},'streams':[{'codec_type':'video'}]}
        with patch.object(p.shutil,'which',return_value='ffprobe'),patch.object(p.subprocess,'run') as run:
            run.return_value.stdout=json.dumps(metadata).encode()
            self.assertFalse(self.scan(b'video','test.mp4')['passed'])

    def test_missing_ffprobe_is_failure(self):
        with patch.dict(p.os.environ,{'FFPROBE':''}),patch.object(p.shutil,'which',return_value=None):
            with self.assertRaisesRegex(p.PrivacyError,'ffprobe'):
                self.scan(b'video','test.mp4')

    def test_review_cannot_be_reused_for_changed_content(self):
        a=self.scan(archive({'report.txt':'reviewed'}));b=self.scan(archive({'report.txt':'changed'}))
        review={'payload_inventory_sha256':a['payload_inventory_sha256'],'reviewed':{}}
        with self.assertRaisesRegex(p.PrivacyError,'Content changed'):
            p.require_review(b,review)

    def test_missing_human_review_blocks_release(self):
        a=self.scan(archive({'report.txt':'public'}))
        with self.assertRaisesRegex(p.PrivacyError,'Missing human'):
            p.require_review(a,{'payload_inventory_sha256':a['payload_inventory_sha256']})


if __name__=='__main__':unittest.main()
