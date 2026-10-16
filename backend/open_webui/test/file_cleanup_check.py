"""Test d'intégration de utils/file_cleanup.py (custom RAGWiame), sur une base et un dossier de données VIDES.

À lancer dans un conteneur jetable de l'image Open WebUI, jamais sur l'instance de prod :
  docker run --rm -e DATA_DIR=/tmp/owui-test -e ENABLE_CHAT_FILE_CLEANUP=true \
    -v <fork>/backend/open_webui/utils/file_cleanup.py:/app/backend/open_webui/utils/file_cleanup.py:ro \
    -v <fork>/backend/open_webui/test/file_cleanup_check.py:/tmp/file_cleanup_check.py:ro \
    --entrypoint python <image> /tmp/file_cleanup_check.py
Code retour 0 si tout passe.
"""

import asyncio
import os
import sys
import time
import uuid

assert os.environ.get('DATA_DIR', '').startswith('/tmp/'), 'DATA_DIR doit être un dossier jetable sous /tmp'
os.makedirs(os.environ['DATA_DIR'], exist_ok=True)
os.environ.setdefault('ENABLE_CHAT_FILE_CLEANUP', 'true')

from open_webui import config  # noqa: E402,F401  (migrations sur la base vide)
from open_webui.config import UPLOAD_DIR  # noqa: E402
from open_webui.internal.db import get_async_db  # noqa: E402
from open_webui.models.chats import ChatForm, Chats  # noqa: E402
from open_webui.models.files import File, FileForm, Files  # noqa: E402
from open_webui.models.knowledge import KnowledgeForm, Knowledges  # noqa: E402
from open_webui.models.users import Users  # noqa: E402
from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT  # noqa: E402
from open_webui.utils import file_cleanup  # noqa: E402
from sqlalchemy import update  # noqa: E402

OLD = time.time() - 3 * 86400  # au-delà du délai de grâce
FAILURES = []


def check(name, condition):
    print(('OK   ' if condition else 'ÉCHEC') + ' ' + name)
    if not condition:
        FAILURES.append(name)


async def new_file(user_id, old=True):
    file_id = str(uuid.uuid4())
    path = os.path.join(UPLOAD_DIR, f'{file_id}_doc.pdf')
    with open(path, 'wb') as fh:
        fh.write(b'%PDF test')
    await Files.insert_new_file(user_id, FileForm(id=file_id, filename='doc.pdf', path=path, meta={'size': 9}))
    if old:
        async with get_async_db() as db:
            await db.execute(update(File).where(File.id == file_id).values(created_at=int(OLD)))
            await db.commit()
        os.utime(path, (OLD, OLD))
    await ASYNC_VECTOR_DB_CLIENT.insert(
        collection_name=f'file-{file_id}',
        items=[{'id': str(uuid.uuid4()), 'text': 'x', 'vector': [0.1, 0.2, 0.3], 'metadata': {'file_id': file_id}}],
    )
    return file_id, path


async def new_chat(user_id, file_ids=(), mention=None, folder_id=None):
    chat_id = str(uuid.uuid4())
    body = {'title': 't', 'messages': [], 'files': [{'id': mention}] if mention else []}
    await Chats.insert_new_chat(chat_id, user_id, ChatForm(chat=body, folder_id=folder_id))
    if file_ids:
        await Chats.insert_chat_files(chat_id, 'm1', list(file_ids), user_id)
    return chat_id


async def gone(file_id, path):
    return (
        await Files.get_file_by_id(file_id) is None
        and not os.path.exists(path)
        and not await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=f'file-{file_id}')
    )


async def kept(file_id, path):
    return await Files.get_file_by_id(file_id) is not None and os.path.exists(path)


async def main():
    user = await Users.insert_new_user(str(uuid.uuid4()), 'Test', f'{uuid.uuid4().hex[:8]}@test.local', role='admin')
    uid = user.id

    # A : conversation supprimée, fichier utilisé nulle part ailleurs → supprimé partout
    f1, p1 = await new_file(uid)
    c1 = await new_chat(uid, [f1])
    ids = await file_cleanup.chat_file_ids(chat_ids=[c1])
    check('A relève les fichiers de la conversation', ids == {f1})
    await Chats.delete_chat_by_id(c1)
    deleted, kept_count = await file_cleanup.delete_unreferenced(ids)
    check('A supprime le fichier (base, disque, collection)', deleted == 1 and await gone(f1, p1))

    # A : fichier partagé entre deux conversations → gardé
    f2, p2 = await new_file(uid)
    c2a, c2b = await new_chat(uid, [f2]), await new_chat(uid, [f2])
    ids = await file_cleanup.chat_file_ids(chat_ids=[c2a])
    await Chats.delete_chat_by_id(c2a)
    await file_cleanup.delete_unreferenced(ids)
    check('A garde un fichier encore lié à une autre conversation', await kept(f2, p2))

    # A : fichier cité seulement dans le JSON d'une conversation existante (ancien format) → gardé
    f3, p3 = await new_file(uid)
    c3 = await new_chat(uid, [f3])
    await new_chat(uid, mention=f3)
    ids = await file_cleanup.chat_file_ids(chat_ids=[c3])
    await Chats.delete_chat_by_id(c3)
    await file_cleanup.delete_unreferenced(ids)
    check('A garde un fichier cité dans le texte d\'une autre conversation', await kept(f3, p3))

    # A : fichier d'une base de connaissances → gardé
    f4, p4 = await new_file(uid)
    c4 = await new_chat(uid, [f4])
    kb = await Knowledges.insert_new_knowledge(uid, KnowledgeForm(name='kb', description='d'))
    await Knowledges.add_file_to_knowledge_by_id(kb.id, f4, uid)
    ids = await file_cleanup.chat_file_ids(chat_ids=[c4])
    await Chats.delete_chat_by_id(c4)
    await file_cleanup.delete_unreferenced(ids)
    check('A garde un fichier d\'une base de connaissances', await kept(f4, p4))

    # A : toutes les conversations d'un dossier
    f5, p5 = await new_file(uid)
    from open_webui.models.folders import FolderForm, Folders

    folder = await Folders.insert_new_folder(uid, FolderForm(name='dossier'))
    await new_chat(uid, [f5], folder_id=folder.id)
    ids = await file_cleanup.chat_file_ids(user_id=uid, folder_ids=[folder.id])
    check('A relève les fichiers des conversations d\'un dossier', ids == {f5})

    # A coupé : rien n'est relevé
    file_cleanup.ENABLE_CHAT_FILE_CLEANUP = False
    check('A coupé : aucun fichier relevé', await file_cleanup.chat_file_ids(chat_ids=[c2b]) == set())
    file_cleanup.ENABLE_CHAT_FILE_CLEANUP = True

    # B : orphelins, lien mort, upload sans ligne, collection sans fichier, fichier récent
    f6, p6 = await new_file(uid)  # orphelin ancien, lié à une conversation supprimée « à l'ancienne »
    c6 = await new_chat(uid, [f6])
    await Chats.delete_chat_by_id(c6)  # sans passer par A : le lien chat_file reste (SQLite)
    f7, p7 = await new_file(uid)  # orphelin ancien, jamais lié
    f8, p8 = await new_file(uid, old=False)  # orphelin récent : délai de grâce
    stray = os.path.join(UPLOAD_DIR, f'{uuid.uuid4()}_perdu.xlsx')
    with open(stray, 'wb') as fh:
        fh.write(b'x')
    os.utime(stray, (OLD, OLD))
    stray_collection = f'file-{uuid.uuid4()}'
    await ASYNC_VECTOR_DB_CLIENT.insert(
        collection_name=stray_collection,
        items=[{'id': str(uuid.uuid4()), 'text': 'x', 'vector': [0.1, 0.2, 0.3], 'metadata': {'source': 'perdu'}}],
    )

    report = await file_cleanup.prune(execute=False)
    orphan_ids = {line.split()[0] for line in report.orphans}
    check('B simulation : les 2 orphelins anciens trouvés', orphan_ids == {f6[:8], f7[:8]})
    check('B simulation : lien mort compté', report.dead_links >= 1)
    check('B simulation : upload sans ligne trouvé', os.path.basename(stray) in report.stray_uploads)
    check('B simulation : collection sans fichier trouvée', stray_collection in report.stray_collections)
    check('B simulation : rien supprimé', await kept(f6, p6) and await kept(f7, p7) and os.path.exists(stray))

    report = await file_cleanup.prune(execute=True)
    check('B exécution : orphelins supprimés partout', report.deleted_files == 2 and await gone(f6, p6) and await gone(f7, p7))
    check('B exécution : orphelin récent gardé', await kept(f8, p8))
    check('B exécution : upload sans ligne supprimé', not os.path.exists(stray))
    check('B exécution : collection sans fichier supprimée',
          not await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=stray_collection))
    check('B exécution : fichiers utilisés gardés', all([await kept(f2, p2), await kept(f3, p3), await kept(f4, p4)]))
    check('B exécution : aucune erreur', report.errors == 0)
    report = await file_cleanup.prune(execute=False)
    check('B : second passage sans orphelin ancien ni lien mort', not report.orphans and report.dead_links == 0)

    # Planification : prochaine heure
    import datetime as dt

    check('heure du prochain passage', file_cleanup._seconds_until(3, dt.datetime(2026, 1, 1, 4, 0)) == 23 * 3600)


asyncio.run(main())
print(f'{len(FAILURES)} échec(s)')
sys.exit(1 if FAILURES else 0)
