"""Export hashes from already downloaded audio assets; never run a model."""
import argparse
import hashlib
import json
from pathlib import Path


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda:source.read(1_048_576),b''): digest.update(block)
    return digest.hexdigest()


def export(recipe_path,root,output='manifest.json'):
    recipe_path,root=Path(recipe_path).resolve(),Path(root).resolve()
    recipe=json.loads(recipe_path.read_text(encoding='utf-8-sig'))
    destination=(root/output).resolve()
    if destination.parent!=root or destination.exists():
        raise ValueError('Choose a new manifest filename directly inside the audio asset folder')
    files={}
    for name,relative in recipe['files'].items():
        path=(root/relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Download/extract the required audio file first: '+relative)
        files[name]=dict(path=relative,sha256=sha256(path),bytes=path.stat().st_size)
    tokens={line.split()[0] for line in (root/recipe['files']['kws_tokens']).read_text(encoding='utf-8').splitlines() if line.strip()}
    for line in (root/recipe['files']['keywords']).read_text(encoding='utf-8').splitlines():
        if line.strip() and any(token not in tokens for token in line.split('@')[0].split()):
            raise ValueError('Wake phrase contains a token outside the chosen KWS vocabulary')
    archives=[]
    for source in recipe['archives']:
        path=(root/source['name']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('Keep the original downloaded archive for provenance: '+source['name'])
        if path.stat().st_size!=source['bytes']:
            raise ValueError('Audio archive size differs from the recorded official metadata')
        digest=sha256(path)
        if source['publisher_sha256'] and digest!=source['publisher_sha256']:
            raise ValueError('Audio archive differs from its publisher digest')
        archives.append(dict(source,export_sha256=digest))
    manifest=dict(schema_version=1,files=files,archives=archives,
        recipe_sha256=sha256(recipe_path),standalone_source=recipe['standalone_source'],
        hash_origin='trusted_local_export; publisher verification only where a publisher digest is supplied',
        inference_tested=False)
    destination.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    checksums=destination.with_suffix('.SHA256SUMS.txt')
    lines=[value['sha256']+'  '+value['path'] for value in files.values()]
    lines.append(sha256(destination)+'  '+destination.name)
    with checksums.open('x',encoding='utf-8',newline='\n') as target:
        target.write('\n'.join(lines)+'\n')
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipe',default='examples/models/audio-recipe.json')
    parser.add_argument('--root',default='models/audio')
    parser.add_argument('--output',default='manifest.json')
    args=parser.parse_args()
    print(export(args.recipe,args.root,args.output))
