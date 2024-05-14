# -*- coding: utf-8 -*-
# Time       : 2024/4/27 19:15
# Author     : QIN2DIM
# GitHub     : https://github.com/QIN2DIM
# Description:
import os
import sys
import time
from typing import List, Dict, Any
from urllib.parse import urlparse

import dotenv
import httpx
from loguru import logger
from pydantic import BaseModel, Field
from tqdm import tqdm

dotenv.load_dotenv()


class UploadDocumentResponse(BaseModel):
    document: Dict[str, Any] = Field(default_factory=dict)
    batch: str = Field(...)


class DifyFireDrop:
    def __init__(
        self,
        separator: str | None = None,
        dify_base_url: str = "http://192.168.1.180/v1",
        api_key: str | None = None,
    ):
        self.my_separator = separator or "\n\n------------\n\n"

        if not (_dify_dataset_api_key := os.getenv("DIFY_DATABASE_API_KEY", api_key)):
            parser = urlparse(dify_base_url)
            lu = f"{parser.scheme}://{parser.netloc}/datasets?category=api"
            logger.error(f"DIFY_DATABASE_API_KEY 缺失，去授权 API 密钥 {lu}")
            sys.exit()

        self._headers = {"Authorization": f"Bearer {_dify_dataset_api_key}"}
        self._dify_base_url = dify_base_url
        self._client = httpx.Client(base_url=self._dify_base_url, headers=self._headers)

    def _document_preprocess_payload(self, *, name: str = "", text: str = ""):
        payload = {
            "name": name,
            "text": text,
            "indexing_technique": "high_quality",
            "process_rule": {
                "mode": "custom",
                "rules": {
                    "pre_processing_rules": [
                        {"id": "remove_extra_spaces", "enabled": False},
                        {"id": "remove_urls_emails", "enabled": False},
                    ],
                    "segmentation": {"separator": self.my_separator, "max_tokens": 1000},
                },
            },
        }
        return payload

    def _delete_document(self, dataset_id: str, document_id: str):
        url = f"/datasets/{dataset_id}/documents/{document_id}"
        res = self._client.delete(url)
        res.raise_for_status()

    def _update_document_by_text(
        self, dataset_id: str, document_id: str, *, table_name: str, text: str
    ) -> UploadDocumentResponse | None:
        url = f"/datasets/{dataset_id}/documents/{document_id}/update_by_text"
        payload = self._document_preprocess_payload(name=table_name, text=text)
        res = self._client.post(url, json=payload, timeout=30)
        try:
            res.raise_for_status()
        except httpx.HTTPStatusError:
            logger.error(
                f"更新文檔失敗，请检查 document 是否已归档，已归档的 document 无法更新 - {table_name=}"
            )
            return

        udr = UploadDocumentResponse(**res.json())
        # document_name = f"{table_name}.txt"
        # logger.debug(f"通过文本更新文档 - {document_name=}")
        return udr

    def _create_document_by_text(
        self, dataset_id: str, *, table_name: str, text: str
    ) -> UploadDocumentResponse:
        """
        通过文本创建知识库文档

        自动创建一个 [self.table_name].txt 的知识库文档文件
        Args:
            dataset_id:
            text:

        Returns:

        """
        url = f"/datasets/{dataset_id}/document/create_by_text"
        payload = self._document_preprocess_payload(name=table_name, text=text)
        res = self._client.post(url, json=payload)
        res.raise_for_status()

        udr = UploadDocumentResponse(**res.json())
        # document_name = f"{table_name}.txt"
        # logger.success(f"通过文本创建知识库文档 - {document_name=}")
        return udr

    def _hook_knowledge_dataset(self, db_name: str) -> str:
        res = self._client.get("/datasets", params={"limit": "100"})
        datasets = res.json()["data"]
        for dataset in datasets:
            if dataset["name"] == db_name:
                dataset_id = dataset["id"]
                logger.success(f"获取知识库Id - Name={db_name} Id={dataset_id}")
                return dataset_id

        logger.warning(
            "知识库不存在！使用 RootAPI 创建的知识库在 Dify 中不可见，请使用 ROOT 账号手动将知识库权限设为<团队成员可见>"
        )
        res = self._client.post("/datasets", json={"name": db_name})
        logger.success(f"创建知识库 - {res.json()}")

        return self._hook_knowledge_dataset(db_name)

    def _sync_document_id(self, dataset_id: str, table_name: str) -> str | None:
        documents = self._list_documents(dataset_id, table_name)
        document_name = f"{table_name}.txt"

        for document in documents:
            if document["name"] == document_name:
                document_id = document["id"]
                # logger.debug(f"获取知识库文档Id - {document_name=} {document_id=}")
                return document_id

    def _list_documents(
        self, dataset_id: str, table_name: str | None = None
    ) -> List[Dict[str, Any]]:
        params = {"keyword": table_name, "limit": "100"}
        res = self._client.get(f"/datasets/{dataset_id}/documents", params=params)

        documents = res.json()["data"]
        # logger.success("获取知识库文档列表")
        return documents

    def _sync_indexing_status(self, dataset_id: str, batch: str):
        url = f"/datasets/{dataset_id}/documents/{batch}/indexing-status"

        progress = tqdm(total=1, desc=f"Embedding")

        while True:
            res = self._client.get(url)
            data = res.json()["data"][0]
            progress.total = data["total_segments"]
            progress.update(data["completed_segments"])
            status = data["indexing_status"]

            progress.postfix = f"Indexing Status: {status}"
            time.sleep(1)

            if status in ["error", "completed"]:
                break

    def embed_knowledge(
        self, table_to_knowledge: Dict[str, str], *, db_name: str, force_override: bool = False
    ):
        """
        通过文本更新文档。

        - 若知识库不存在则新建知识库
        - 若文档不存在则新建文档
        - 若存在文档则更新文档
        - self.table_name AS document_name

        Args:
            force_override: 删除文档再创建
            table_to_knowledge: (table_name, KnowledgeCard) .to_knowledge_card() 返回的已编排好的知识卡片
            db_name: 统一存放数据集市业务数据的知识库名称，默认为 "数据集市"

        Returns:

        """
        if not table_to_knowledge:
            logger.error("不可以添加空的文档")
            return

        # [操作/新建] 知识库，获取操作句柄
        dataset_id = self._hook_knowledge_dataset(db_name=db_name)

        # 通过文本 [更新/创建] 文档，获取操作句柄
        response_seq = []
        tasks = tqdm(table_to_knowledge.items())
        for table_name, knowledge_card in tasks:
            tasks.postfix = f"{db_name=} {table_name=}"
            if document_id := self._sync_document_id(dataset_id, table_name):
                if force_override:
                    self._delete_document(dataset_id, document_id)
                    response = self._create_document_by_text(
                        dataset_id, table_name=table_name, text=knowledge_card
                    )
                else:
                    response = self._update_document_by_text(
                        dataset_id, document_id, table_name=table_name, text=knowledge_card
                    )
            else:
                response = self._create_document_by_text(
                    dataset_id, table_name=table_name, text=knowledge_card
                )
            response_seq.append(response)

    def delete_all_document(self, *, db_name: str):
        # [操作/新建] 知识库，获取操作句柄
        dataset_id = self._hook_knowledge_dataset(db_name=db_name)

        docs = self._list_documents(dataset_id)
        for doc in docs:
            try:
                self._delete_document(dataset_id, doc["id"])
                logger.debug(f"Delete document - {doc=}")
            except httpx.HTTPStatusError as err:
                logger.warning(f"Failed to delete document - {doc['name']=} {err=}")
        logger.success(f"Delete all document - count={len(docs)}")
