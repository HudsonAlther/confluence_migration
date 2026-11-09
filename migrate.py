#!/usr/bin/env python3
"""Copy a Confluence Cloud page tree onto an existing Server or Data Center page.

Dry-run by default. Python 3.10+. pip install requests playwright

Put the sites and page ids in .env or pass flags. Credentials stay in .env:

    SOURCE_URL=https://yoursite.atlassian.net/wiki
    SOURCE_EMAIL=cloud-user@company.com
    SOURCE_TOKEN=atlassian-api-token
    SOURCE_PAGE_ID=123456
    TARGET_URL=https://confluence.example.com
    TARGET_PARENT_ID=789012
"""
import argparse, json, os, re, sys, time, mimetypes, base64
from pathlib import Path
from urllib.parse import urljoin, urlparse, unquote
import requests

SOURCE=''
TARGET=''
SOURCE_ID=''
TARGET_PARENT_ID=''
EXPECT_SPACE=''
HERE=Path(__file__).resolve().parent
STATE=HERE / 'confluence-migration-state.json'
REPORT=HERE / 'confluence-migration-report.json'
ENV_FILE=HERE / '.env'
SESSION=HERE / 'confluence-migration-session.json'
LOGIN_TIMEOUT=300

def load_env(path):
    """Load KEY=VALUE pairs. Existing environment variables win. Empty values are ignored."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding='utf-8').splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line=line[7:].strip()
        if '=' not in line:
            continue
        key,value=line.split('=',1)
        key,value=key.strip(),value.strip()
        if len(value)>=2 and value[0]==value[-1] and value[0] in '"\'':
            value=value[1:-1]
        if key and value and key not in os.environ:
            os.environ[key]=value

class Client:
    def __init__(self, base, prefix):
        self.base=base.rstrip('/')
        self.s=requests.Session()
        token=os.getenv(prefix+'_TOKEN')
        email=os.getenv(prefix+'_EMAIL')
        if not token: raise RuntimeError(f'Missing {prefix}_TOKEN environment variable')
        if email: self.s.auth=(email,token)
        else: self.s.headers['Authorization']='Bearer '+token
        self.s.headers['Accept']='application/json'
    def call(self, method, path, **kw):
        url=path if path.startswith('http') else self.base+path
        for attempt in range(5):
            r=self.s.request(method,url,timeout=90,**kw)
            if r.status_code in (429,502,503,504) and attempt<4:
                time.sleep(min(30,2**attempt));continue
            if not r.ok:
                if r.status_code==403 and 'cannot access Confluence' in r.text:
                    raise RuntimeError(
                        'Cloud accepted the source login, but that account is not allowed to use Confluence. '
                        'Open the source page in a browser, create an API token while signed in as that user, '
                        'and put that same email in SOURCE_EMAIL and the new token in SOURCE_TOKEN.')
                raise RuntimeError(f'{method} {url}: HTTP {r.status_code}: {r.text[:350]}')
            return r
    def api(self, method, path, **kw):
        r=self.call(method,'/rest/api'+path,**kw)
        try:
            return r.json()
        except ValueError:
            host=urlparse(r.url).hostname or r.url
            kind=r.headers.get('content-type','unknown')
            raise RuntimeError(
                f'{method} {path} returned HTTP {r.status_code} ({kind}) from {host}, not JSON. '
                'The LDR site stopped at a login page.')
    def pages(self, path, params=None):
        params=dict(params or {});params.setdefault('limit',100);params.setdefault('start',0)
        while True:
            result=self.api('GET',path,params=params)
            items=result.get('results',[])
            yield from items
            if len(items)<params['limit']:break
            params['start']+=len(items)

def looks_like_json(response):
    kind=response.headers.get('content-type','')
    return response.ok and 'json' in kind.lower()

class BrowserResponse:
    def __init__(self,response):
        self.status_code=response.status
        self.ok=response.ok
        self.url=response.url
        self.headers=response.headers
        self.content=response.body()
        self.text=self.content.decode('utf-8','replace')
    def json(self):
        return json.loads(self.text)

class PageResponse:
    def __init__(self,data):
        self.status_code=data['status']
        self.ok=200<=data['status']<300
        self.url=data.get('url') or ''
        self.text=data.get('text') or ''
        self.content=self.text.encode('utf-8')
        self.headers={'content-type':data.get('contentType') or ''}
    def json(self):
        return json.loads(self.text)

class BrowserClient:
    """LDR calls stay inside the signed in browser. requests cannot pass that login wall."""
    def __init__(self,context,page,base):
        self.context=context
        self.page=page
        self.base=base.rstrip('/')
    def _same_origin(self):
        host=urlparse(self.base).hostname
        if urlparse(self.page.url).hostname==host:
            return
        self.page.goto(self.base+'/',wait_until='domcontentloaded',timeout=45000)
    def _from_page(self,method,url,headers,body):
        # JSON writes from Playwright's HTTP client are treated as untrusted browser
        # requests. fetch() on the wiki page sends a same origin Origin.
        self._same_origin()
        data=self.page.evaluate(
            """async ({url, method, headers, body}) => {
                const response = await fetch(url, {
                    method, headers, body: body ?? undefined, credentials: 'include',
                });
                return {status: response.status, url: response.url, text: await response.text(),
                        contentType: response.headers.get('content-type') || ''};
            }""",
            {'url':url,'method':method,'headers':headers,'body':body},
        )
        return PageResponse(data)
    def _upload_from_page(self,url,filename,mime,data):
        # Same XSRF rule as page creates: the upload has to come from the wiki page.
        self._same_origin()
        result=self.page.evaluate(
            """async ({url, filename, mime, b64}) => {
                const raw = atob(b64);
                const bytes = new Uint8Array(raw.length);
                const step = 0x8000;
                for (let i = 0; i < raw.length; i += step) {
                    const end = Math.min(i + step, raw.length);
                    for (let j = i; j < end; j++) bytes[j] = raw.charCodeAt(j);
                }
                const form = new FormData();
                form.append('file', new Blob([bytes], {type: mime || 'application/octet-stream'}), filename);
                const response = await fetch(url, {
                    method: 'POST', credentials: 'include', body: form,
                    headers: {'Accept': 'application/json', 'X-Atlassian-Token': 'no-check'},
                });
                return {status: response.status, url: response.url, text: await response.text(),
                        contentType: response.headers.get('content-type') || ''};
            }""",
            {'url':url,'filename':filename,'mime':mime or 'application/octet-stream',
             'b64':base64.b64encode(data).decode('ascii')},
        )
        return PageResponse(result)
    def call(self,method,path,**kw):
        url=path if path.startswith('http') else self.base+path
        headers={'Accept':'application/json','X-Atlassian-Token':'no-check'}
        headers.update(kw.get('headers') or {})
        params=kw.get('params')
        if params:
            params={k:(v if isinstance(v,(str,float,bool)) else str(v)) for k,v in params.items()}
            query='&'.join(f'{k}={requests.utils.quote(str(v),safe="")}' for k,v in params.items())
            url+=('&' if '?' in url else '?')+query
        timeout=int(kw.get('timeout',90)*1000)
        files=kw.get('files')
        for attempt in range(5):
            if files:
                title,data,mime=files['file']
                wrapped=self._upload_from_page(url,title,mime,data)
            elif method in ('POST','PUT','DELETE'):
                if 'json' in kw:
                    headers['Content-Type']='application/json'
                    body=json.dumps(kw['json'])
                else:
                    body=None
                wrapped=self._from_page(method,url,headers,body)
            else:
                response=self.context.request.fetch(url,method=method,headers=headers,timeout=timeout)
                wrapped=BrowserResponse(response)
            if wrapped.status_code in (429,502,503,504) and attempt<4:
                time.sleep(min(30,2**attempt));continue
            if not wrapped.ok:
                raise RuntimeError(f'{method} {url}: HTTP {wrapped.status_code}: {wrapped.text[:350]}')
            return wrapped
    def api(self,method,path,**kw):
        r=self.call(method,'/rest/api'+path,**kw)
        try:
            return r.json()
        except ValueError:
            host=urlparse(r.url).hostname or r.url
            kind=r.headers.get('content-type','unknown')
            raise RuntimeError(
                f'{method} {path} returned HTTP {r.status_code} ({kind}) from {host}, not JSON. '
                'The LDR site stopped at a login page.')
    def pages(self,path,params=None):
        params=dict(params or {});params.setdefault('limit',100);params.setdefault('start',0)
        while True:
            result=self.api('GET',path,params=params)
            items=result.get('results',[])
            yield from items
            if len(items)<params['limit']:break
            params['start']+=len(items)

class TargetBrowser:
    def __init__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError('LDR login needs the playwright package. Run: py -m pip install playwright') from e
        self._pw=sync_playwright().start()
        self._browser=None
        last_error=None
        for channel in ('msedge','chrome'):
            try:
                self._browser=self._pw.chromium.launch(headless=False,channel=channel)
                break
            except Exception as e:
                last_error=e
        if self._browser is None:
            self._pw.stop()
            raise RuntimeError(f'Could not open Edge or Chrome for LDR login: {last_error}')
        state=str(SESSION) if SESSION.is_file() else None
        try:
            self._context=self._browser.new_context(storage_state=state)
        except Exception:
            self._context=self._browser.new_context()
        page=self._context.new_page()
        self.client=BrowserClient(self._context,page,TARGET)
        try:
            page.goto(TARGET+'/',wait_until='domcontentloaded',timeout=45000)
        except Exception as e:
            print('Browser is open. Finish the LDR login if the page is still loading.',e)
        try:
            self._wait_for_login()
        except Exception:
            self.close()
            raise
    def _wait_for_login(self):
        url=f'{TARGET}/rest/api/content/{TARGET_PARENT_ID}?expand=space'
        deadline=time.time()+LOGIN_TIMEOUT
        announced=False
        while time.time()<deadline:
            response=self._context.request.get(url,timeout=30000)
            if looks_like_json(response):
                self._context.storage_state(path=str(SESSION))
                print('LDR login accepted.')
                return
            if not announced:
                print('Sign in to LDR in the browser window. The script continues after login succeeds.')
                announced=True
            time.sleep(2)
        raise RuntimeError('Timed out after 5 minutes waiting for the LDR login to finish.')
    def close(self):
        if getattr(self,'_closed',False):return
        self._closed=True
        try:
            if self._browser is not None:
                try:self._context.storage_state(path=str(SESSION))
                except Exception:pass
                self._browser.close()
        finally:
            self._pw.stop()

def v2_json(src,path):
    return src.call('GET',src.base+'/api/v2'+path).json()

def attach_parents(src,nodes,ancestors_of,root):
    """Keep Cloud folders in the tree. Data Center has no folders, so they are copied as pages."""
    for pid,chain in ancestors_of.items():
        if pid not in nodes:
            continue
        for aid,atype,title in chain:
            if atype=='page' or aid in nodes:
                continue
            nodes[aid]={'id':aid,'title':title or 'Folder','kind':'folder','space':nodes[pid].get('space'),
                        'body':'<p></p>','parent':None}
    # Prefer the live parent type bc folders are omitted by the page only descendant API.
    queue=list(nodes)
    seen=set()
    while queue:
        cid=queue.pop(0)
        if cid in seen:
            continue
        seen.add(cid)
        if cid==root:
            nodes[cid]['parent']=None
            continue
        kind='folders' if nodes[cid].get('kind')=='folder' else 'pages'
        try:
            info=v2_json(src,f'/{kind}/{cid}')
        except RuntimeError:
            info=None
        parent_id=str(info.get('parentId')) if info and info.get('parentId') else None
        parent_type=(info or {}).get('parentType')
        if parent_type=='folder' and parent_id and parent_id not in nodes:
            try:
                folder=v2_json(src,f'/folders/{parent_id}')
            except RuntimeError:
                folder=None
            if folder:
                nodes[parent_id]={'id':parent_id,'title':folder.get('title') or 'Folder','kind':'folder',
                                  'space':nodes[cid].get('space'),'body':'<p></p>','parent':None}
                queue.append(parent_id)
        if parent_id and parent_id in nodes:
            nodes[cid]['parent']=parent_id
        elif 'parent' not in nodes[cid] or nodes[cid].get('parent') is None:
            parent=None
            for aid,atype,_title in ancestors_of.get(cid,[]):
                if aid in nodes:
                    parent=aid
            nodes[cid]['parent']=parent
    adopt_children(src,nodes,root)
    nodes[root]['parent']=None

def v2_collect(src,path):
    items=[]
    query=''
    while True:
        data=v2_json(src,path+query)
        if isinstance(data,list):
            return data
        items.extend(data.get('results') or [])
        nxt=(data.get('_links') or {}).get('next')
        if not nxt or '?' not in nxt:
            break
        query='?'+nxt.split('?',1)[1]
    return items

def adopt_children(src,nodes,root):
    """Parent each page from the folder or page that lists it as a direct child."""
    queue=list(nodes)
    seen=set()
    while queue:
        cid=queue.pop(0)
        if cid in seen or cid not in nodes:
            continue
        seen.add(cid)
        kind='folders' if nodes[cid].get('kind')=='folder' else 'pages'
        try:
            children=v2_collect(src,f'/{kind}/{cid}/children?limit=250')
        except RuntimeError:
            continue
        for index,child in enumerate(children):
            if child.get('status') not in (None,'current'):
                continue
            child_id=str(child.get('id') or '')
            if not child_id or child_id==root:
                continue
            ctype=(child.get('type') or '').lower()
            if ctype=='folder':
                if child_id not in nodes:
                    nodes[child_id]={'id':child_id,'title':child.get('title') or 'Folder','kind':'folder',
                                     'space':nodes[cid].get('space'),'body':'<p></p>','parent':cid,'position':index}
                    queue.append(child_id)
                else:
                    nodes[child_id]['parent']=cid
                    nodes[child_id]['kind']='folder'
                    nodes[child_id]['position']=index
                continue
            if child_id not in nodes:
                page=fetch_page(src,child_id)
                if not page:
                    continue
                page['kind']='page'
                nodes[child_id]=page
                queue.append(child_id)
            nodes[child_id]['parent']=cid
            nodes[child_id]['position']=index
            queue.append(child_id)

def fetch_page(src,pid):
    p=src.api('GET',f'/content/{pid}',params={'expand':'body.storage,space,version'})
    if p.get('type')!='page' or p.get('status') not in (None,'current'):
        return None
    return {'id':str(p['id']),'title':p['title'],'space':p['space']['key'],
            'body':p['body']['storage']['value'],'version':p['version']['number']}

def get_tree(src,root):
    """All current pages under the source root, parents before children.

    Child listings omit pages that the descendant API still returns, so the
    descendant list is the source of truth.
    """
    root_page=fetch_page(src,root)
    if not root_page:
        raise RuntimeError(f'Source {root} is not a current page')
    nodes={root:root_page}
    ancestors_of={}
    for item in src.pages(f'/content/{root}/descendant/page',{'expand':'ancestors','limit':100}):
        pid=str(item['id'])
        if item.get('status') not in (None,'current'):
            continue
        ancestors_of[pid]=[(str(a['id']), a.get('type') or 'page', a.get('title') or '') for a in item.get('ancestors') or []]
        page=fetch_page(src,pid)
        if page:
            page['kind']='page'
            nodes[pid]=page
    root_page['kind']='page'
    attach_parents(src, nodes, ancestors_of, root)
    ordered=[]
    seen=set()
    def kids(pid):
        rows=[node for node in nodes.values() if node.get('parent')==pid]
        rows.sort(key=lambda node: (node.get('position',10**9), node.get('title') or ''))
        return rows
    def walk(pid,stack):
        if pid in seen:
            return
        if pid in stack:
            raise RuntimeError(f'Page parent cycle at {pid}')
        page=nodes[pid]
        seen.add(pid)
        ordered.append(page)
        for child in kids(pid):
            walk(child['id'],stack|{pid})
    walk(root,set())
    for pid in list(nodes):
        walk(pid,set())
    return ordered

def current_parent_id(page):
    ancestors=[str(a['id']) for a in page.get('ancestors') or [] if a.get('type')=='page']
    return ancestors[-1] if ancestors else None

def place_page(dst,page_id,parent_id,title):
    """Move using only the direct parent. The full ancestor chain relocated pages to the space root."""
    current=dst.api('GET',f'/content/{page_id}',params={'expand':'version,ancestors,body.storage'})
    if current_parent_id(current)==str(parent_id) and current.get('title')==title:
        return
    payload={'id':str(page_id),'type':'page','title':title,
             'version':{'number':current['version']['number']+1},
             'ancestors':[{'id':str(parent_id)}],
             'body':current['body']['storage']}
    dst.api('PUT',f'/content/{page_id}',json=payload)
    print('Placed:',title)

def update_body(dst,page_id,title,body,report):
    """Rewrite text without ancestors, so a rejected body cannot move the page."""
    current=dst.api('GET',f'/content/{page_id}',params={'expand':'version,body.storage'})
    if current['body']['storage']['value']==body and current.get('title')==title:
        return
    payload={'id':str(page_id),'type':'page','title':title,
             'version':{'number':current['version']['number']+1},
             'body':{'storage':{'value':body,'representation':'storage'}}}
    try:
        dst.api('PUT',f'/content/{page_id}',json=payload)
        print('Updated:',title)
    except RuntimeError as e:
        if 'HTTP 500' not in str(e):
            raise
        report['warnings'].append(f'{title}: Cloud body was rejected, so the existing text was kept')
        print('Kept existing text:',title)

def search_username(dst,cql):
    data=dst.api('GET','/search',params={'cql':cql,'limit':'5'})
    found=[]
    for item in data.get('results') or []:
        user=item.get('user') or item.get('content') or {}
        username=user.get('username')
        if username and username not in found:
            found.append(username)
    if len(found)==1:
        return found[0]
    return None

def dc_username(dst,person):
    username=person.get('username')
    if username:
        return username
    email=(person.get('email') or '').replace('"','')
    if email:
        match=search_username(dst,f'type=user AND user.email="{email}"')
        if match:
            return match
    name=(person.get('displayName') or '').replace('"','')
    if name:
        return search_username(dst,f'type=user AND user.fullname="{name}"')
    return None

def copy_restrictions(src,dst,old,new,report,title):
    try:
        data=src.api('GET',f'/content/{old}/restriction',params={'expand':'restrictions.user,restrictions.group','limit':'100'})
    except RuntimeError as e:
        report['warnings'].append(f'Restrictions on {title}: {e}')
        return
    if isinstance(data,dict) and 'results' not in data and ('read' in data or 'update' in data):
        items=[data[key] for key in ('read','update') if isinstance(data.get(key),dict)]
    else:
        items=data.get('results') or []
    me=None
    ops=[]
    for item in items:
        operation=item.get('operation')
        box=item.get('restrictions') or {}
        users=((box.get('user') or {}).get('results')) or []
        groups=((box.get('group') or {}).get('results')) or []
        if not users and not groups:
            continue
        mapped_groups=[{'type':'group','name':g['name']} for g in groups if g.get('name')]
        mapped_users=[]
        for person in users:
            username=dc_username(dst,person)
            if username:
                mapped_users.append({'type':'known','username':username})
            else:
                who=person.get('displayName') or person.get('email') or 'a Cloud account'
                report['warnings'].append(f'Restrictions on {title}: no LDR user matched {who}')
        if not mapped_users and not mapped_groups:
            report['warnings'].append(f'Restrictions on {title}: {operation} was not copied because nobody matched')
            continue
        if me is None:
            try:me=(dst.api('GET','/user/current') or {}).get('username')
            except RuntimeError:me=None
        if me and not any(u.get('username')==me for u in mapped_users):
            mapped_users.append({'type':'known','username':me})
        ops.append({'operation':operation,'restrictions':{'user':mapped_users,'group':mapped_groups}})
    if not ops:
        return
    try:
        dst.api('PUT',f'/content/{new}/restriction',json=ops)
        print('Restrictions:',title)
    except RuntimeError as e:
        report['warnings'].append(f'Restrictions on {title}: {e}')

def get_attachments(client,pid):
    return list(client.pages(f'/content/{pid}/child/attachment'))

def copy_labels(src,dst,old,new):
    labels=[{'prefix':x.get('prefix') or 'global','name':x['name']}
            for x in src.pages(f'/content/{old}/label') if x.get('name')]
    if labels:
        dst.api('POST',f'/content/{new}/label',json=labels)

def save(state):
    STATE.write_text(json.dumps(state,indent=2),encoding='utf-8')

def safe_filename(name):
    name=name.split('?',1)[0].strip()
    name=re.sub(r'[^\w.() -]','_',name).strip(' .')
    return (name or 'attachment')[:180]

def attachment_renames(src,pid):
    renames={}
    for att in get_attachments(src,pid):
        title=att['title']
        safe=safe_filename(title)
        if safe!=title:
            renames[title]=safe
    return renames

def apply_renames(body,renames):
    for old,new in renames.items():
        body=body.replace(f'ri:filename="{old}"',f'ri:filename="{new}"')
        body=body.replace(f"ri:filename='{old}'",f"ri:filename='{new}'")
    return body

def migrate_attachments(src,dst,old,new,state,report):
    attachments=get_attachments(src,old)
    old_map=state.setdefault('attachments',{}).setdefault(old,{})
    existing={x['title']:x for x in get_attachments(dst,new)}
    for att in attachments:
        raw=att['title']
        title=safe_filename(raw)
        if title in old_map or raw in old_map or title in existing:
            report['skipped_attachments'].append({'page':old,'name':title,'reason':'already tracked or exists'})
            continue
        link=att.get('_links',{}).get('download')
        if not link:
            report['warnings'].append(f'No download URL for {old}/{title}');continue
        # Download from authenticated source; never send credentials to other hosts.
        # Cloud download paths start with /download and must keep the /wiki prefix.
        if link.startswith('http'):
            url=link
        else:
            site=urlparse(src.base)
            host=f'{site.scheme}://{site.netloc}'
            url=host+link if link.startswith('/wiki/') else (src.base+link if link.startswith('/') else urljoin(src.base+'/',link))
        if urlparse(url).hostname != urlparse(src.base).hostname:
            report['warnings'].append(f'External attachment URL skipped: {title}');continue
        data=src.call('GET',url).content
        # X-Atlassian-Token required by many Confluence instances for attachment upload.
        headers={'X-Atlassian-Token':'no-check'}
        mime=mimetypes.guess_type(title)[0] or 'application/octet-stream'
        resp=dst.call('POST',f'/rest/api/content/{new}/child/attachment',
                      headers=headers,files={'file':(title,data,mime)})
        out=resp.json().get('results',[])
        old_map[title]=str(out[0]['id']) if out else 'uploaded'
        report['uploaded_attachments'].append({'page':old,'name':title})
        save(state)

def unwrap_adf(match):
    block=match.group(0)
    content=re.search(r'<ac:adf-content>(.*)</ac:adf-content>',block,re.S)
    if content and content.group(1).strip():
        return content.group(1).strip()
    fallback=re.search(r'<ac:adf-fallback>(.*)</ac:adf-fallback>',block,re.S)
    if fallback and fallback.group(1).strip():
        return fallback.group(1).strip()
    return '<p><em>Cloud-only block omitted.</em></p>'

def sanitize(body):
    """Keep the inner text of Cloud-only blocks. Server has no ADF extensions."""
    if not body:
        return '<p></p>'
    pattern=re.compile(r'<ac:adf-extension\b(?:(?!<ac:adf-extension\b).)*?</ac:adf-extension>',re.S)
    previous=None
    while previous!=body:
        previous=body
        body=pattern.sub(unwrap_adf,body)
    body=re.sub(r'<ac:link\b[^>]*>\s*<ri:user\b[^>]*(?:/>|>\s*</ri:user>)\s*</ac:link>','Cloud user',body,flags=re.S)
    body=re.sub(r'<ri:user\b[^>]*(?:/>|>\s*</ri:user>)','Cloud user',body)
    body=re.sub(r'<ac:image\b[^>]*>\s*<ri:url\b(?![^>]*\bri:value="[^"]+)[^>]*/>\s*</ac:image>','',body,flags=re.S)
    body=re.sub(r'<ri:url\b(?![^>]*\bri:value="[^"]+)[^>]*/>','',body)
    body=re.sub(r'\s+(?:(?:ri|ac):)?local-id="[^"]*"','',body)
    body=re.sub(r'<ac:structured-macro\b[^>]*\bac:name="profile"[^>]*>.*?</ac:structured-macro>',
                'Cloud user',body,flags=re.S)
    return body

def preserved_body(body):
    safe=(body or '').replace(']]>',']]]]><![CDATA[>')
    return ('<p>This page could not be stored in its original format. '
            'The source storage is preserved below.</p>'
            f'<ac:structured-macro ac:name="noformat"><ac:plain-text-body><![CDATA[{safe}]]></ac:plain-text-body></ac:structured-macro>')

def create_page(dst,data,report,title):
    last=None
    for attempt,value in enumerate((data['body']['storage']['value'], preserved_body(data['body']['storage']['value']),
                                    '<p>Content could not be migrated automatically.</p>')):
        payload=json.loads(json.dumps(data))
        payload['body']['storage']['value']=value
        try:
            created=dst.api('POST','/content',json=payload)
            if attempt:
                report['warnings'].append(f'{title}: stored with a fallback body after the original format was rejected')
            return created
        except RuntimeError as e:
            last=e
            if 'HTTP 500' not in str(e):
                raise
    raise last

def rewrite(body,mapping):
    for old,new in mapping.items():
        body=re.sub(r'(?<=/pages/)'+re.escape(old)+r'(?!\d)',new,body)
        body=re.sub(r'(?<=pageId=)'+re.escape(old)+r'(?!\d)',new,body)
        body=re.sub(r'(<ri:page\b[^>]*\bri:content-id=["\'])'+re.escape(old)+r'(["\'])',r'\g<1>'+new+r'\2',body)
    source=SOURCE.rstrip('/')
    target=TARGET.rstrip('/')
    body=body.replace(source+'/spaces/',target+'/spaces/')
    body=body.replace(source+'/display/',target+'/display/')
    if source.endswith('/wiki'):
        origin=source[:-5]
        body=body.replace(origin+'/wiki/spaces/',target+'/spaces/')
        body=body.replace(origin+'/wiki/display/',target+'/display/')
    return body

def require_url(value,label):
    value=(value or '').strip().rstrip('/')
    if not value:
        raise RuntimeError(f'Missing {label}. Set it in .env or pass the matching flag.')
    parts=urlparse(value)
    if parts.scheme not in ('https','http') or not parts.netloc or parts.username or parts.password:
        raise RuntimeError(f'{label} must be an http(s) URL with no embedded username or password')
    return value

def require_page_id(value,label):
    value=(value or '').strip()
    if not re.fullmatch(r'[0-9]{1,20}',value):
        raise RuntimeError(f'{label} must be a numeric Confluence page id')
    return value

def migration_name(explicit):
    chosen=(explicit or os.getenv('MIGRATION_NAME') or '').strip()
    if not chosen:
        legacy=HERE / 'observatory-migration-state.json'
        generic=HERE / 'confluence-migration-state.json'
        if legacy.is_file() and not generic.is_file():
            print('Using existing state file observatory-migration-state.json')
            chosen='observatory-migration'
        else:
            chosen='confluence-migration'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,40}',chosen):
        raise RuntimeError('--name must be 1-41 letters, numbers, hyphens, or underscores')
    return chosen

def apply_config(args):
    global SOURCE,TARGET,SOURCE_ID,TARGET_PARENT_ID,EXPECT_SPACE,STATE,REPORT,SESSION
    SOURCE=require_url(args.source_url or os.getenv('SOURCE_URL'),'SOURCE_URL or --source-url')
    TARGET=require_url(args.target_url or os.getenv('TARGET_URL'),'TARGET_URL or --target-url')
    SOURCE_ID=require_page_id(args.source_page or os.getenv('SOURCE_PAGE_ID'),'SOURCE_PAGE_ID or --source-page')
    TARGET_PARENT_ID=require_page_id(args.target_parent or os.getenv('TARGET_PARENT_ID'),'TARGET_PARENT_ID or --target-parent')
    EXPECT_SPACE=(args.expect_space if args.expect_space is not None else os.getenv('EXPECT_SPACE') or '').strip()
    if EXPECT_SPACE and not re.fullmatch(r'[A-Za-z0-9]{1,30}',EXPECT_SPACE):
        raise RuntimeError('EXPECT_SPACE must be a Confluence space key')
    name=migration_name(args.name)
    STATE=HERE / f'{name}-state.json'
    REPORT=HERE / f'{name}-report.json'
    SESSION=HERE / f'{name}-session.json'

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--source-url',help='Cloud wiki base, for example https://site.atlassian.net/wiki')
    ap.add_argument('--target-url',help='Server or Data Center base, for example https://confluence.example.com')
    ap.add_argument('--source-page',help='Cloud page id whose tree is copied, including that page')
    ap.add_argument('--target-parent',help='Existing destination page id that will contain the copied tree')
    ap.add_argument('--expect-space',help='Abort unless the destination page is in this space key')
    ap.add_argument('--name',help='Prefix for the state, report, and browser session files')
    ap.add_argument('--apply',action='store_true',help='Create pages and attachments (default: dry run)')
    ap.add_argument('--skip-attachments',action='store_true')
    args=ap.parse_args()
    load_env(ENV_FILE)
    apply_config(args)
    print('Source page',SOURCE_ID,'on',SOURCE)
    print('Destination parent',TARGET_PARENT_ID,'on',TARGET)
    src=Client(SOURCE,'SOURCE')
    target=TargetBrowser()
    try:
        dst=target.client
        dest=dst.api('GET',f'/content/{TARGET_PARENT_ID}',params={'expand':'space'})
        _run(args,src,dst,dest)
    finally:
        target.close()

def _run(args,src,dst,dest):
    if dest.get('type')!='page':raise RuntimeError('Destination is not a page')
    dest_space=dest['space']['key']
    print('Destination:',dest['title'],'in space',dest_space)
    if EXPECT_SPACE and dest_space!=EXPECT_SPACE:
        raise RuntimeError(f'Expected destination space {EXPECT_SPACE}, got {dest_space}')
    tree=get_tree(src,SOURCE_ID)
    report={'mode':'apply' if args.apply else 'dry-run','source_count':len(tree),
            'pages':[{'id':p['id'],'parent':p['parent'],'title':p['title']} for p in tree],
            'created_pages':[],'skipped_pages':[],'uploaded_attachments':[],
            'skipped_attachments':[],'warnings':[]}
    by_id={p['id']:p for p in tree}
    def depth(pid):
        d=0
        seen=set()
        while by_id[pid].get('parent') in by_id and pid not in seen:
            seen.add(pid)
            pid=by_id[pid]['parent']
            d+=1
        return d
    for p in tree:
        label=' [folder]' if p.get('kind')=='folder' else ''
        print(f"{'  '*depth(p['id'])}- {p['title']} ({p['id']}){label}")
    if not args.apply:
        for p in tree:
            if p.get('kind')=='folder':
                continue
            try:
                count=len(get_attachments(src,p['id']))
                if count:print(f"{'  '*depth(p['id'])}  {count} attachment(s)")
            except Exception as e:report['warnings'].append(f"Attachment inventory {p['id']}: {e}")
        REPORT.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print('\nDRY RUN ONLY: no changes made. Review report, then run --apply.')
        return
    state=json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {'pages':{},'attachments':{}}
    mapping=state.setdefault('pages',{})
    for p in tree:
        old=p['id']; parent=mapping.get(p['parent']) if p['parent'] else TARGET_PARENT_ID
        if not parent:raise RuntimeError(f'Missing migrated parent for {old}')
        if old in mapping:
            report['skipped_pages'].append({'id':old,'reason':'already in state'})
            continue
        matches=[x for x in dst.pages(f'/content/{parent}/child/page') if x['title']==p['title']]
        if matches:
            raise RuntimeError(f"Page '{p['title']}' already exists under destination parent {parent}. "
                               'Stop to review rather than overwrite or adopt automatically.')
        data={'type':'page','title':p['title'],'space':{'key':dest_space},
              'ancestors':[{'id':str(parent)}],
              'body':{'storage':{'value':sanitize(p['body']),'representation':'storage'}}}
        created=create_page(dst,data,report,p['title'])
        mapping[old]=str(created['id']);save(state)
        report['created_pages'].append({'source':old,'destination':mapping[old],'title':p['title']})
        print('Created:',p['title'],mapping[old])
    for p in tree:
        old=p['id'];new=mapping[old]
        parent=mapping.get(p['parent']) if p['parent'] else TARGET_PARENT_ID
        renames={} if p.get('kind')=='folder' else attachment_renames(src,old)
        body=apply_renames(rewrite(sanitize(p['body']),mapping),renames)
        try:place_page(dst,new,parent,p['title'])
        except Exception as e:report['warnings'].append(f'Place {p["title"]}: {e}')
        try:update_body(dst,new,p['title'],body,report)
        except Exception as e:report['warnings'].append(f'Body {p["title"]}: {e}')
        if p.get('kind')=='folder' or args.skip_attachments:
            continue
        try:migrate_attachments(src,dst,old,new,state,report)
        except Exception as e:report['warnings'].append(f'Attachments on {p["title"]}: {e}')
        try:copy_labels(src,dst,old,new)
        except Exception as e:report['warnings'].append(f'Labels on {p["title"]}: {e}')
    REPORT.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('\nFinished. Inspect report and verify pages, links, macros, and attachments manually.')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print('ERROR:',e,file=sys.stderr)
        sys.exit(1)
