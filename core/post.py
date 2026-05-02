import json
import typing

import aiosqlite

from .config import PluginConfig
from .model import Comment, Post

post_key = typing.Literal[
    "id",
    "tid",
    "uin",
    "name",
    "gin",
    "status",
    "anon",
    "text",
    "images",
    "videos",
    "create_time",
    "rt_con",
    "comments",
    "extra_text",
]


class PostDB:
    # 允许查询的列
    ALLOWED_QUERY_KEYS = {
        "id",
        "tid",
        "uin",
        "name",
        "gin",
        "status",
        "anon",
        "text",
        "images",
        "videos",
        "create_time",
        "rt_con",
        "comments",
        "extra_text",
    }

    def __init__(self, config: PluginConfig):
        self.db_path = config.db_path

    @staticmethod
    def _row_to_post(row) -> Post:
        return Post(
            id=row[0],
            tid=row[1],
            uin=row[2],
            name=row[3],
            gin=row[4],
            text=row[5],
            images=json.loads(row[6]),
            videos=json.loads(row[7]),
            anon=bool(row[8]),
            status=row[9],
            create_time=row[10],
            rt_con=row[11],
            comments=[Comment.model_validate(c) for c in json.loads(row[12])],
            extra_text=row[13],
        )

    @staticmethod
    def _encode_urls(urls: list[str]) -> str:
        return json.dumps(urls, ensure_ascii=False)

    async def initialize(self):
        """初始化数据库"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tid TEXT UNIQUE,
                    uin INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    gin INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    images TEXT NOT NULL CHECK(json_valid(images)),
                    videos TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(videos)),
                    anon INTEGER NOT NULL CHECK(anon IN (0,1)),
                    status TEXT NOT NULL,
                    create_time INTEGER NOT NULL,
                    rt_con TEXT NOT NULL DEFAULT '',
                    comments TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(comments)),
                    extra_text TEXT
                )
            """)
            await db.commit()

    async def add(self, post: Post) -> int:
        """添加稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            cur = await db.execute(
                """
                INSERT INTO posts (tid, uin, name, gin, text, images, videos, anon, status, create_time, rt_con, comments, extra_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                ),
            )
            await db.commit()
            last_id = cur.lastrowid  # 获取自增ID
            assert last_id is not None
            return last_id

    async def get(self, value, key: post_key = "id") -> Post | None:
        """
        根据指定字段查询一条稿件记录，默认按 id 查询。
        当 key=='id' 且 value==-1 时，返回 id 最大的那一条记录。
        """
        if value is None:
            raise ValueError("必须提供查询值")
        if key not in PostDB.ALLOWED_QUERY_KEYS:
            raise ValueError(f"不允许的查询字段: {key}")
        async with aiosqlite.connect(self.db_path) as db:
            # 关键判断：-1 代表取最大 ID
            if key == "id" and value == -1:
                query = "SELECT * FROM posts ORDER BY id DESC LIMIT 1"
                async with db.execute(query) as cursor:
                    row = await cursor.fetchone()
                    return self._row_to_post(row) if row else None
            # 普通查询保持原逻辑
            query = f"SELECT * FROM posts WHERE {key} = ? LIMIT 1"
            async with db.execute(query, (value,)) as cursor:
                row = await cursor.fetchone()
                return self._row_to_post(row) if row else None

    async def update(self, post: Post) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            comment_dicts = [c.model_dump() for c in post.comments]
            await db.execute(
                """
                UPDATE posts SET
                    tid = ?, uin = ?, name = ?, gin = ?, text = ?,
                    images = ?, videos = ?, anon = ?, status = ?,
                    create_time = ?, rt_con = ?, comments = ?, extra_text = ?
                WHERE id = ?
                """,
                (
                    post.tid or None,
                    post.uin,
                    post.name,
                    post.gin,
                    post.text,
                    self._encode_urls(post.images),
                    self._encode_urls(post.videos),
                    int(post.anon),
                    post.status,
                    post.create_time,
                    post.rt_con,
                    json.dumps(comment_dicts, ensure_ascii=False),
                    post.extra_text,
                    post.id,
                ),
            )
            await db.commit()

    async def delete(self, post_id: int) -> int:
        """删除稿件"""
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
            await db.commit()
            return cur.rowcount
