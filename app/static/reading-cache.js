(() => {
  'use strict';

  const DB_NAME = 'nas-reader-reading-cache';
  const DB_VERSION = 1;
  const META = 'meta';
  const PAGES = 'pages';
  const FILES = 'files';
  const SHORT_TTL = 3 * 60 * 60 * 1000;
  const ACTIVE_TTL = 24 * 60 * 60 * 1000;
  const SOFT_LIMIT = 5 * 1024 * 1024 * 1024;

  let dbPromise = null;

  function req(request) {
    return new Promise((resolve, reject) => {
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error || new Error('IndexedDB request failed'));
    });
  }

  function txDone(tx) {
    return new Promise((resolve, reject) => {
      tx.oncomplete = () => resolve();
      tx.onerror = () => reject(tx.error || new Error('IndexedDB transaction failed'));
      tx.onabort = () => reject(tx.error || new Error('IndexedDB transaction aborted'));
    });
  }

  function openDb() {
    if (!('indexedDB' in window)) return Promise.reject(new Error('IndexedDB unavailable'));
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const r = indexedDB.open(DB_NAME, DB_VERSION);
      r.onupgradeneeded = () => {
        const db = r.result;
        if (!db.objectStoreNames.contains(META)) {
          const s = db.createObjectStore(META, { keyPath: 'key' });
          s.createIndex('bookId', 'bookId', { unique: false });
          s.createIndex('lastAccess', 'lastAccess', { unique: false });
        }
        if (!db.objectStoreNames.contains(PAGES)) {
          const s = db.createObjectStore(PAGES, { keyPath: 'id' });
          s.createIndex('bookKey', 'bookKey', { unique: false });
        }
        if (!db.objectStoreNames.contains(FILES)) {
          db.createObjectStore(FILES, { keyPath: 'key' });
        }
      };
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error || new Error('Unable to open reading cache'));
    });
    return dbPromise;
  }

  function keyFor(book) {
    const stamp = Math.round(Number(book.mtime || 0) * 1000);
    return `${book.id}:${book.file_type || 'pdf'}:${Number(book.size || 0)}:${stamp}`;
  }


  async function deleteBookKey(key) {
    const db = await openDb();
    const tx = db.transaction([META, FILES, PAGES], 'readwrite');
    tx.objectStore(META).delete(key);
    tx.objectStore(FILES).delete(key);
    const index = tx.objectStore(PAGES).index('bookKey');
    await new Promise((resolve, reject) => {
      const r = index.openCursor(IDBKeyRange.only(key));
      r.onsuccess = () => {
        const c = r.result;
        if (!c) return resolve();
        c.delete();
        c.continue();
      };
      r.onerror = () => reject(r.error);
    });
    await txDone(tx);
  }

  async function cleanupStaleVersions(book) {
    const db = await openDb();
    const current = keyFor(book);
    const tx = db.transaction(META, 'readonly');
    const metas = await req(tx.objectStore(META).index('bookId').getAll(Number(book.id)));
    await txDone(tx);
    for (const m of metas || []) {
      if (m.key !== current) await deleteBookKey(m.key);
    }
  }

  async function cleanupExpired() {
    try {
      const db = await openDb();
      const tx = db.transaction(META, 'readonly');
      const all = await req(tx.objectStore(META).getAll());
      await txDone(tx);
      const now = Date.now();
      for (const m of all || []) {
        if (!m.expiresAt || m.expiresAt <= now) await deleteBookKey(m.key);
      }
      return true;
    } catch {
      return false;
    }
  }

  async function touchBook(book) {
    try {
      await cleanupStaleVersions(book);
      await cleanupExpired();
      const db = await openDb();
      const key = keyFor(book);
      const tx = db.transaction(META, 'readwrite');
      const store = tx.objectStore(META);
      let meta = await req(store.get(key));
      const now = Date.now();
      if (!meta || meta.expiresAt <= now) {
        meta = { key, bookId: Number(book.id), fileType: book.file_type || 'pdf', qualified: false, createdAt: now, lastAccess: now, expiresAt: now + SHORT_TTL, bytes: 0 };
      } else {
        meta.lastAccess = now;
        meta.expiresAt = now + (meta.qualified ? ACTIVE_TTL : SHORT_TTL);
      }
      store.put(meta);
      await txDone(tx);
      return meta;
    } catch {
      return null;
    }
  }

  async function qualify(book) {
    try {
      const db = await openDb();
      const key = keyFor(book);
      const tx = db.transaction(META, 'readwrite');
      const store = tx.objectStore(META);
      let meta = await req(store.get(key));
      const now = Date.now();
      meta = meta || { key, bookId: Number(book.id), fileType: book.file_type || 'pdf', createdAt: now, bytes: 0 };
      meta.qualified = true;
      meta.lastAccess = now;
      meta.expiresAt = now + ACTIVE_TTL;
      store.put(meta);
      await txDone(tx);
      return meta;
    } catch {
      return null;
    }
  }

  async function metaFor(book) {
    try {
      const db = await openDb();
      const key = keyFor(book);
      const tx = db.transaction(META, 'readonly');
      const meta = await req(tx.objectStore(META).get(key));
      await txDone(tx);
      if (!meta || meta.expiresAt <= Date.now()) return null;
      return meta;
    } catch {
      return null;
    }
  }

  async function bumpBytes(book, delta) {
    if (!delta) return;
    const db = await openDb();
    const key = keyFor(book);
    const tx = db.transaction(META, 'readwrite');
    const store = tx.objectStore(META);
    const meta = await req(store.get(key));
    if (meta) {
      const now=Date.now();
      meta.bytes = Math.max(0, Number(meta.bytes || 0) + Number(delta || 0));
      meta.lastAccess = now;
      meta.expiresAt = now + (meta.qualified ? ACTIVE_TTL : SHORT_TTL);
      store.put(meta);
    }
    await txDone(tx);
  }

  async function getPage(book, page, width) {
    try {
      const meta = await metaFor(book);
      if (!meta) return null;
      const db = await openDb();
      const id = `${meta.key}|${Number(width)}|${Number(page)}`;
      const tx = db.transaction(PAGES, 'readonly');
      const row = await req(tx.objectStore(PAGES).get(id));
      await txDone(tx);
      return row?.blob || null;
    } catch {
      return null;
    }
  }

  async function hasPage(book, page, width) {
    try {
      const meta = await metaFor(book);
      if (!meta) return false;
      const db = await openDb();
      const id = `${meta.key}|${Number(width)}|${Number(page)}`;
      const tx = db.transaction(PAGES, 'readonly');
      const key = await req(tx.objectStore(PAGES).getKey(id));
      await txDone(tx);
      return key != null;
    } catch {
      return false;
    }
  }

  async function storageAllows(extraBytes) {
    try {
      if (!navigator.storage?.estimate) return true;
      const e = await navigator.storage.estimate();
      if (!e.quota || e.usage == null) return true;
      return (e.usage + Number(extraBytes || 0)) < e.quota * 0.88;
    } catch {
      return true;
    }
  }

  async function evictOldest(exceptKey) {
    const db = await openDb();
    const tx = db.transaction(META, 'readonly');
    const all = await req(tx.objectStore(META).getAll());
    await txDone(tx);
    const candidates = (all || []).filter(x => x.key !== exceptKey).sort((a, b) => Number(a.lastAccess || 0) - Number(b.lastAccess || 0));
    if (candidates[0]) {
      await deleteBookKey(candidates[0].key);
      return true;
    }
    return false;
  }

  async function ensureRoom(extraBytes, exceptKey) {
    const extra=Number(extraBytes||0);
    for(let i=0;i<20;i++){
      const own=await totalBytes();
      if(own+extra<=SOFT_LIMIT && await storageAllows(extra))return true;
      if(!(await evictOldest(exceptKey)))return await storageAllows(extra);
    }
    return false;
  }

  async function totalBytes() {
    try {
      const db = await openDb();
      const tx = db.transaction(META, 'readonly');
      const all = await req(tx.objectStore(META).getAll());
      await txDone(tx);
      return (all || []).reduce((s, x) => s + Number(x.bytes || 0), 0);
    } catch {
      return 0;
    }
  }

  async function putPage(book, page, width, blob) {
    if (!blob) return false;
    try {
      const meta = await metaFor(book) || await touchBook(book);
      if (!meta) return false;
      const db = await openDb();
      const id = `${meta.key}|${Number(width)}|${Number(page)}`;
      let tx = db.transaction(PAGES, 'readonly');
      const existing = await req(tx.objectStore(PAGES).get(id));
      await txDone(tx);
      if (existing) return true;
      if (!(await ensureRoom(blob.size, meta.key))) return false;
      try {
        tx = db.transaction(PAGES, 'readwrite');
        tx.objectStore(PAGES).put({ id, bookKey: meta.key, page: Number(page), width: Number(width), blob, size: blob.size, createdAt: Date.now() });
        await txDone(tx);
      } catch (e) {
        if (e?.name === 'QuotaExceededError') {
          if (!(await evictOldest(meta.key))) return false;
          tx = db.transaction(PAGES, 'readwrite');
          tx.objectStore(PAGES).put({ id, bookKey: meta.key, page: Number(page), width: Number(width), blob, size: blob.size, createdAt: Date.now() });
          await txDone(tx);
        } else throw e;
      }
      await bumpBytes(book, blob.size);
      return true;
    } catch {
      return false;
    }
  }

  async function getFile(book) {
    try {
      const meta = await metaFor(book);
      if (!meta) return null;
      const db = await openDb();
      const tx = db.transaction(FILES, 'readonly');
      const row = await req(tx.objectStore(FILES).get(meta.key));
      await txDone(tx);
      return row?.blob || null;
    } catch {
      return null;
    }
  }

  async function putFile(book, blob) {
    if (!blob) return false;
    try {
      const meta = await metaFor(book) || await touchBook(book);
      if (!meta) return false;
      const db = await openDb();
      let tx = db.transaction(FILES, 'readonly');
      const existing = await req(tx.objectStore(FILES).get(meta.key));
      await txDone(tx);
      if (existing) return true;
      if (!(await ensureRoom(blob.size, meta.key))) return false;
      tx = db.transaction(FILES, 'readwrite');
      tx.objectStore(FILES).put({ key: meta.key, bookId: Number(book.id), blob, size: blob.size, createdAt: Date.now() });
      await txDone(tx);
      await bumpBytes(book, blob.size);
      return true;
    } catch {
      return false;
    }
  }

  async function clearAll() {
    try {
      const db = await openDb();
      const tx = db.transaction([META, PAGES, FILES], 'readwrite');
      tx.objectStore(META).clear();
      tx.objectStore(PAGES).clear();
      tx.objectStore(FILES).clear();
      await txDone(tx);
      return true;
    } catch {
      return false;
    }
  }

  async function stats() {
    try {
      await cleanupExpired();
      const db = await openDb();
      const tx = db.transaction(META, 'readonly');
      const all = await req(tx.objectStore(META).getAll());
      await txDone(tx);
      const bytes = (all || []).reduce((s, x) => s + Number(x.bytes || 0), 0);
      return { books: (all || []).length, bytes };
    } catch {
      return { books: 0, bytes: 0 };
    }
  }

  window.ReaderCache = {
    SHORT_TTL,
    ACTIVE_TTL,
    keyFor,
    touchBook,
    qualify,
    metaFor,
    getPage,
    hasPage,
    putPage,
    getFile,
    putFile,
    cleanupExpired,
    clearAll,
    stats,
    supported: 'indexedDB' in window,
  };
})();
