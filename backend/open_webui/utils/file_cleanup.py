"""Fichiers des conversations supprimées et fichiers orphelins (custom RAGWiame).

A. Suppression d'une conversation (seule, toutes, dossier) : ses fichiers que plus rien n'utilise sont supprimés
   juste après, en tâche de fond (ENABLE_CHAT_FILE_CLEANUP=true). Une erreur ici n'empêche jamais la suppression
   de la conversation.
B. Ménage périodique, dans le serveur (FILE_CLEANUP_MODE=dry-run|execute) : fichiers que plus rien n'utilise,
   fichiers du dossier des uploads sans ligne en base, collections vectorielles « file-<id> » sans fichier.
   Premier passage FILE_CLEANUP_FIRST_DELAY secondes après le démarrage, puis chaque jour à FILE_CLEANUP_HOUR
   (heure du conteneur). Dans le serveur et pas dans un second processus : Chroma en mode fichier ne supporte pas
   deux processus, et importer open_webui.config lance les migrations.

Un fichier n'est supprimé que si son identifiant n'apparaît nulle part : liens chat_file vers une conversation qui
existe, puis toutes les colonnes texte de toutes les tables (conversations et anciens JSON, messages, conversations
partagées, bases de connaissances, canaux, notes, dossiers, modèles, droits d'accès…). Dans le doute, on garde.
Les fichiers de moins de FILE_CLEANUP_GRACE_HOURS ne sont jamais touchés par B : l'envoi d'un fichier et le message
qui l'utilise sont deux requêtes distinctes.

SQLite n'applique pas ON DELETE CASCADE (pas de PRAGMA foreign_keys) : les liens chat_file des conversations
supprimées restent en base. Ils ne comptent pas comme une utilisation et sont effacés ici.
Sur Chroma, ASYNC_VECTOR_DB_CLIENT.delete(collection_name=…) sans ids ni filtre ne supprime rien : la collection
d'un fichier est supprimée avec delete_collection.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from dataclasses import dataclass, field

from sqlalchemy import BigInteger, Boolean, Float, Integer, Numeric, SmallInteger, delete, func, inspect, select, text

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        log.warning('FILE_CLEANUP: %s invalide, valeur par défaut %s', name, default)
        return default


ENABLE_CHAT_FILE_CLEANUP = os.environ.get('ENABLE_CHAT_FILE_CLEANUP', 'false').strip().lower() == 'true'
FILE_CLEANUP_MODE = os.environ.get('FILE_CLEANUP_MODE', 'off').strip().lower()  # off | dry-run | execute
FILE_CLEANUP_HOUR = int(_env_float('FILE_CLEANUP_HOUR', 3)) % 24
FILE_CLEANUP_GRACE_HOURS = _env_float('FILE_CLEANUP_GRACE_HOURS', 24)
FILE_CLEANUP_FIRST_DELAY = _env_float('FILE_CLEANUP_FIRST_DELAY', 300)

_NON_TEXT_TYPES = (Integer, BigInteger, SmallInteger, Boolean, Float, Numeric)
_SKIPPED_TABLES = {'file', 'chat_file', 'alembic_version', 'migratehistory'}
_TASKS: set[asyncio.Task] = set()  # tâches de fond gardées en vie jusqu'à leur fin


@dataclass
class CleanupReport:
    execute: bool
    orphans: list[str] = field(default_factory=list)  # « id[:8] nom (âge, taille, propriétaire) »
    deleted_files: int = 0
    dead_links: int = 0
    stray_uploads: list[str] = field(default_factory=list)
    stray_collections: list[str] = field(default_factory=list)
    errors: int = 0


def _text_columns(sync_conn) -> list[tuple[str, str]]:
    insp = inspect(sync_conn)
    columns = []
    for table in insp.get_table_names():
        if table in _SKIPPED_TABLES:
            continue
        for column in insp.get_columns(table):
            if not isinstance(column['type'], _NON_TEXT_TYPES):
                columns.append((table, column['name']))
    return columns


async def find_referenced(file_ids: set[str]) -> set[str]:
    """Identifiants cités quelque part (lien vers une conversation existante ou texte d'une table)."""
    from open_webui.internal.db import get_async_db
    from open_webui.models.chats import Chat, ChatFile

    remaining = set(file_ids)
    found: set[str] = set()
    if not remaining:
        return found
    async with get_async_db() as db:
        result = await db.execute(
            select(ChatFile.file_id).where(ChatFile.file_id.in_(remaining), ChatFile.chat_id.in_(select(Chat.id)))
        )
        found |= set(result.scalars().all())
        remaining -= found
        connection = await db.connection()
        for table, column in await connection.run_sync(_text_columns):
            if not remaining:
                break
            # Identifiants issus de l'introspection de la base, jamais d'une entrée utilisateur.
            result = await db.execute(
                text(f'SELECT CAST("{column}" AS TEXT) FROM "{table}" WHERE "{column}" IS NOT NULL')
            )
            for (value,) in result:
                if not value:
                    continue
                hits = {file_id for file_id in remaining if file_id in value}
                if hits:
                    found |= hits
                    remaining -= hits
                    if not remaining:
                        break
    return found


async def _drop_collection(name: str) -> None:
    from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT

    if await ASYNC_VECTOR_DB_CLIENT.has_collection(collection_name=name):
        await ASYNC_VECTOR_DB_CLIENT.delete_collection(collection_name=name)


async def _drop_dead_chat_links() -> int:
    """Liens chat_file vers une conversation qui n'existe plus (ON DELETE CASCADE non appliqué par SQLite)."""
    from open_webui.internal.db import get_async_db
    from open_webui.models.chats import Chat, ChatFile

    async with get_async_db() as db:
        result = await db.execute(delete(ChatFile).where(ChatFile.chat_id.not_in(select(Chat.id))))
        await db.commit()
        return result.rowcount or 0


async def delete_file_everywhere(file) -> bool:
    """Ligne en base, fichier stocké, collection vectorielle. À n'appeler que sur un fichier que rien ne cite
    (donc dans aucune base de connaissances, contrairement à routers/files.py:delete_file_by_id)."""
    from open_webui.internal.db import get_async_db
    from open_webui.models.chats import ChatFile
    from open_webui.models.files import Files
    from open_webui.storage.provider import Storage

    async with get_async_db() as db:
        await db.execute(delete(ChatFile).where(ChatFile.file_id == file.id))
        await db.commit()
    if not await Files.delete_file_by_id(file.id):
        return False
    if file.path:
        try:
            await asyncio.to_thread(Storage.delete_file, file.path)
        except Exception as exc:  # fichier déjà absent du disque : la ligne est partie, c'est l'essentiel
            log.warning('FILE_CLEANUP: fichier stocké non supprimé %s (%s)', file.id, exc)
    try:
        await _drop_collection(f'file-{file.id}')
    except Exception as exc:
        log.warning('FILE_CLEANUP: collection file-%s non supprimée (%s)', file.id, exc)
    return True


async def delete_unreferenced(file_ids: set[str]) -> tuple[int, int]:
    """Supprime ceux de ces fichiers que plus rien ne cite ; (supprimés, gardés)."""
    from open_webui.models.files import Files

    await _drop_dead_chat_links()
    referenced = await find_referenced(file_ids)
    deleted = 0
    for file_id in file_ids - referenced:
        file = await Files.get_file_by_id(file_id)
        if file and await delete_file_everywhere(file):
            deleted += 1
    return deleted, len(file_ids) - deleted


# --- A : suppression d'une conversation ---


async def chat_file_ids(
    *, chat_ids: list[str] | None = None, user_id: str | None = None, folder_ids: list[str] | None = None
) -> set[str]:
    """Fichiers liés aux conversations sur le point d'être supprimées (à relever AVANT la suppression).
    Ensemble vide si la fonction est coupée ou en cas d'erreur."""
    if not ENABLE_CHAT_FILE_CLEANUP:
        return set()
    from open_webui.internal.db import get_async_db
    from open_webui.models.chats import Chat, ChatFile

    try:
        query = select(ChatFile.file_id)
        if chat_ids is not None:
            query = query.where(ChatFile.chat_id.in_(chat_ids))
        else:
            chats = select(Chat.id).where(Chat.user_id == user_id)
            if folder_ids is not None:
                chats = chats.where(Chat.folder_id.in_(folder_ids))
            query = query.where(ChatFile.chat_id.in_(chats))
        async with get_async_db() as db:
            return set((await db.execute(query)).scalars().all())
    except Exception:
        log.exception('FILE_CLEANUP: fichiers des conversations non relevés')
        return set()


async def _cleanup_after_chat_deletion(file_ids: set[str]) -> None:
    try:
        deleted, kept = await delete_unreferenced(file_ids)
        log.info('FILE_CLEANUP: conversation supprimée, fichier(s) supprimé(s)=%d gardé(s)=%d', deleted, kept)
    except Exception:
        log.exception('FILE_CLEANUP: nettoyage après suppression de conversation en échec')


def cleanup_files_later(file_ids: set[str]) -> None:
    """Après la suppression des conversations : nettoyage en tâche de fond, la réponse n'attend pas."""
    if not ENABLE_CHAT_FILE_CLEANUP or not file_ids:
        return
    task = asyncio.create_task(_cleanup_after_chat_deletion(set(file_ids)))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


# --- B : ménage périodique ---


def _collection_names() -> list[str]:
    """Noms des collections, pour Chroma seulement (pas de liste dans l'API commune) ; [] sinon."""
    from open_webui.retrieval.vector.async_client import ASYNC_VECTOR_DB_CLIENT

    client = getattr(ASYNC_VECTOR_DB_CLIENT.sync, 'client', None)
    if client is None or not hasattr(client, 'list_collections'):
        return []
    return [c if isinstance(c, str) else getattr(c, 'name', '') for c in client.list_collections()]


async def prune(execute: bool, grace_hours: float = FILE_CLEANUP_GRACE_HOURS, now: float | None = None) -> CleanupReport:
    from open_webui.config import STORAGE_PROVIDER, UPLOAD_DIR
    from open_webui.internal.db import get_async_db
    from open_webui.models.chats import Chat, ChatFile
    from open_webui.models.files import Files

    now = time.time() if now is None else now
    cutoff = now - grace_hours * 3600
    report = CleanupReport(execute=execute)

    files = await Files.get_files()
    known_ids = {f.id for f in files}
    old = {f.id: f for f in files if (f.created_at or 0) < cutoff}
    referenced = await find_referenced(set(old))
    orphans = [f for file_id, f in old.items() if file_id not in referenced]
    for f in orphans:
        size = (f.meta or {}).get('size') or 0
        report.orphans.append(
            f'{f.id[:8]} {f.filename} ({(now - (f.created_at or 0)) / 86400:.0f} j, {size / 1e6:.2f} Mo, '
            f'propriétaire {(f.user_id or "?")[:8]})'
        )

    async with get_async_db() as db:
        result = await db.execute(
            select(func.count()).select_from(ChatFile).where(ChatFile.chat_id.not_in(select(Chat.id)))
        )
        report.dead_links = result.scalar() or 0

    if STORAGE_PROVIDER == 'local' and os.path.isdir(UPLOAD_DIR):
        known_names = {os.path.basename(f.path) for f in files if f.path}
        for name in sorted(os.listdir(UPLOAD_DIR)):
            path = os.path.join(UPLOAD_DIR, name)
            if name not in known_names and os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                report.stray_uploads.append(name)

    try:
        names = await asyncio.to_thread(_collection_names)
    except Exception as exc:
        log.warning('FILE_CLEANUP: collections non listées (%s)', exc)
        names = []
    report.stray_collections = [n for n in names if n.startswith('file-') and n[5:] not in known_ids]

    if execute:
        await _drop_dead_chat_links()
        for f in orphans:
            try:
                report.deleted_files += 1 if await delete_file_everywhere(f) else 0
            except Exception:
                report.errors += 1
                log.exception('FILE_CLEANUP: fichier %s non supprimé', f.id)
        for name in report.stray_uploads:
            try:
                os.remove(os.path.join(UPLOAD_DIR, name))
            except OSError as exc:
                report.errors += 1
                log.warning('FILE_CLEANUP: upload sans ligne non supprimé %s (%s)', name, exc)
        for name in report.stray_collections:
            try:
                await _drop_collection(name)
            except Exception as exc:
                report.errors += 1
                log.warning('FILE_CLEANUP: collection %s non supprimée (%s)', name, exc)

    mode = 'execute' if execute else 'simulation'
    log.info(
        'FILE_CLEANUP %s : fichiers en base=%d, orphelins=%d (supprimés=%d), liens morts=%d, '
        'uploads sans ligne=%d, collections sans fichier=%d, erreurs=%d',
        mode, len(files), len(orphans), report.deleted_files, report.dead_links,
        len(report.stray_uploads), len(report.stray_collections), report.errors,
    )
    for line in report.orphans:
        log.info('FILE_CLEANUP %s : orphelin %s', mode, line)
    for name in report.stray_uploads:
        log.info('FILE_CLEANUP %s : upload sans ligne %s', mode, name)
    for name in report.stray_collections:
        log.info('FILE_CLEANUP %s : collection sans fichier %s', mode, name)
    return report


def _seconds_until(hour: int, now: dt.datetime | None = None) -> float:
    now = now or dt.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += dt.timedelta(days=1)
    return (target - now).total_seconds()


async def periodic_file_cleanup() -> None:
    """Tâche du cycle de vie de l'application ; ne fait rien si FILE_CLEANUP_MODE n'est ni dry-run ni execute."""
    if FILE_CLEANUP_MODE not in ('dry-run', 'execute'):
        return
    execute = FILE_CLEANUP_MODE == 'execute'
    log.info('FILE_CLEANUP: ménage %s, premier passage dans %.0f s puis chaque jour à %d h',
             FILE_CLEANUP_MODE, FILE_CLEANUP_FIRST_DELAY, FILE_CLEANUP_HOUR)
    await asyncio.sleep(FILE_CLEANUP_FIRST_DELAY)
    while True:
        try:
            await prune(execute=execute)
        except Exception:
            log.exception('FILE_CLEANUP: ménage en échec')
        await asyncio.sleep(_seconds_until(FILE_CLEANUP_HOUR))
