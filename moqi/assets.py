"""Verify transferred model artifacts without importing or running a model."""
import argparse
import hashlib
import json
from pathlib import Path


def verify(manifest_path,required):
    manifest_path=Path(manifest_path).resolve()
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    if type(manifest.get('schema_version')) is not int or manifest['schema_version']!=1:
        raise ValueError('Model manifest needs schema_version 1')
    root=manifest_path.parent
    result={}
    for name in required:
        entry=manifest['files'].get(name)
        if not isinstance(entry,dict) or not isinstance(entry.get('path'),str):
            raise ValueError('Missing model manifest entry: '+name)
        path=(root/entry['path']).resolve()
        digest=entry.get('sha256')
        if (not path.is_relative_to(root) or not path.is_file() or not isinstance(digest,str) or
                len(digest)!=64 or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError('Model file or verified hash is missing: '+name)
        hasher=hashlib.sha256()
        with path.open('rb') as source:
            for block in iter(lambda:source.read(1_048_576),b''): hasher.update(block)
        if hasher.hexdigest()!=digest:
            raise ValueError('Transferred model hash does not match: '+name)
        result[name]=str(path)
    return result


def main():
    parser=argparse.ArgumentParser(description='Verify an exported model manifest; no inference')
    parser.add_argument('manifest')
    args=parser.parse_args()
    value=json.loads(Path(args.manifest).read_text(encoding='utf-8-sig'))
    paths=verify(args.manifest,list(value['files']))
    print(json.dumps(dict(status='hashes_verified_only',files=len(paths),model_inference=False)))


if __name__=='__main__': main()
