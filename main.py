import os
import yaml
import pymysql
import sqlglot

from dotenv import load_dotenv

from langchain_chroma import Chroma
from langchain_google_genai import (
    ChatGoogleGenerativeAI,
    GoogleGenerativeAIEmbeddings,
)

from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnablePassthrough
from langchain_core.output_parsers import StrOutputParser


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

DB_HOST = "localhost"
DB_USER = "root"
DB_PASSWORD = ""
DB_NAME = "llm"


# ============================================================
# SCHEMA DESCRIPTION
# ============================================================

sales_schema_yaml = """
tables:
  - name: dim_products
    description: "Product table"

    columns:
      - name: prod_id
        description: "Primary key"

      - name: prod_title
        description: "Product name"

      - name: category_group
        description: "Category"

      - name: stock_qty
        description: "Current inventory quantity"

  - name: fact_sales_transactions
    description: "Sales transactions"

    columns:
      - name: tx_id
        description: "Transaction id"

      - name: order_date
        description: "Date of order"

      - name: prod_id
        description: "Foreign key to product"

      - name: quantity_sold
        description: "Number of items sold"

      - name: price
        description: "Selling price per item"
"""


# ============================================================
# LOAD SCHEMA
# ============================================================

schema_data = yaml.safe_load(sales_schema_yaml)


# ============================================================
# EMBEDDINGS + CHROMA
# ============================================================

embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001"
)

vector_db = Chroma(
    collection_name="mysql_sales_metadata",
    persist_directory="./chroma_db",
    embedding_function=embeddings,
)

# Add schema only once
if vector_db._collection.count() == 0:

    print("Indexing schema...")

    for table in schema_data["tables"]:

        text = (
            f"Table: {table['name']}. "
            f"Description: {table['description']}. "
        )

        for col in table["columns"]:
            text += (
                f"Column {col['name']} means "
                f"{col['description']}. "
            )

        vector_db.add_texts(
            texts=[text],
            metadatas=[
                {"table_name": table["name"]}
            ],
        )


# ============================================================
# RETRIEVER
# ============================================================

def semantic_schema_retriever(question: str):

    docs = vector_db.similarity_search(
        question,
        k=2
    )

    return "\n\n".join(
        doc.page_content
        for doc in docs
    )


# ============================================================
# PROMPTS & GEMINI CONFIGURATION
# ============================================================

# 1. Prompt for turning natural language to SQL
sql_generation_prompt = ChatPromptTemplate.from_template(
"""
You are an expert MySQL Text-to-SQL assistant.

Convert the question into a valid MySQL query.

Rules:
- Use ONLY the tables and columns provided.
- Return ONLY SQL.
- Never generate: INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, CREATE.

Database Schema:
{schema_context}

Question:
{question}
"""
)

# 2. Prompt for turning raw database numbers into human language
response_synthesis_prompt = ChatPromptTemplate.from_template(
"""
You are a helpful business assistant. Answer the user's question accurately based on the provided MySQL database result. 
Provide a clean, natural language response. Do not output raw tuples or markdown code arrays; list things nicely.

User Question: {question}
SQL Query Used: {sql_query}
Database Result: {db_result}

Answer:
"""
)

# LLM Instance (Updated to gemini-2.5-flash)
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0
)


# ============================================================
# CHAINS
# ============================================================

# SQL Generation Chain
sql_chain = (
    {
        "schema_context": semantic_schema_retriever,
        "question": RunnablePassthrough(),
    }
    | sql_generation_prompt
    | llm
    | StrOutputParser()
)

# Response Synthesis Chain
response_chain = response_synthesis_prompt | llm | StrOutputParser()


# ============================================================
# SECURITY
# ============================================================

def verify_query_safety(sql_query: str):

    try:
        parsed = sqlglot.parse_one(
            sql_query,
            read="mysql"
        )

        if not isinstance(
            parsed,
            (
                sqlglot.exp.Select,
                sqlglot.exp.Union,
            ),
        ):
            return False

        return True

    except Exception:
        return False


# ============================================================
# MYSQL EXECUTION
# ============================================================

def execute_query(sql_query):

    if not verify_query_safety(sql_query):
        return "BLOCKED: Only SELECT statements are allowed."

    try:
        conn = pymysql.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME,
        )

        cursor = conn.cursor()
        cursor.execute(sql_query)
        rows = cursor.fetchall()

        headers = [desc[0] for desc in cursor.description]

        cursor.close()
        conn.close()

        # Build clean textual context for the synthesis LLM
        output = f"Columns: {', '.join(headers)}\n"
        output += "Rows Data:\n"
        for row in rows:
            output += f"{str(row)}\n"

        return output

    except Exception as e:
        return f"MySQL Error: {str(e)}"


# ============================================================
# MAIN LOOP
# ============================================================

if __name__ == "__main__":

    print("\n")
    print("=" * 60)
    print("Gemini Text-to-SQL Agent (with Natural Answers)")
    print("=" * 60)

    while True:

        question = input("\nAsk a question (or 'exit'): ")

        if question.lower() == "exit":
            break

        try:
            # Step 1: Generate the SQL Query
            generated_sql_raw = sql_chain.invoke(question)
            
            # Clean up potential markdown formatting from the raw LLM output
            generated_sql = (
                generated_sql_raw
                .replace("```sql", "")
                .replace("```", "")
                .strip()
            )

            print(f"\n[Debug SQL Execution]:\n{generated_sql}\n")

            # Step 2: Fetch the Raw Database Data
            db_result = execute_query(generated_sql)

            # If our security guardrail blocked it, notify user and skip synthesis
            if "BLOCKED" in db_result:
                print(db_result)
                continue

            # Step 3: Synthesize the final, clean answer
            final_answer = response_chain.invoke({
                "question": question,
                "sql_query": generated_sql,
                "db_result": db_result
            })

            print("-" * 40)
            print(final_answer)
            print("-" * 40)

        except Exception as e:
            print(f"\nAgent Error: {e}")