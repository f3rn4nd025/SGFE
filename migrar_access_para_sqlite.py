import argparse, os, sqlite3, datetime, decimal
import pyodbc

TYPE_MAP = {
    -7: "INTEGER", -6: "INTEGER", 4: "INTEGER", 5: "INTEGER", 2: "REAL", 3: "REAL",
    6: "REAL", 7: "REAL", 8: "TEXT", 9: "TEXT", 10: "TEXT", 11: "INTEGER",
    12: "TEXT", 91: "TEXT", 92: "TEXT", 93: "TEXT", -1: "TEXT", -9: "TEXT", -10: "TEXT",
    -2: "BLOB", -3: "BLOB", -4: "BLOB"
}

def q(name): return '"' + str(name).replace('"','""') + '"'

def norm(v):
    if isinstance(v, decimal.Decimal): return float(v)
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)): return v.isoformat(sep=' ') if hasattr(v,'isoformat') else str(v)
    return v

def main():
    ap=argparse.ArgumentParser(description='Converte o banco SGFE Access para SQLite para publicação no Render.')
    ap.add_argument('--access', default=r'C:\FG FOTOS\SGFE.accdb')
    ap.add_argument('--saida', default=os.path.join('dados','sgfe_admin.sqlite3'))
    args=ap.parse_args()
    if not os.path.exists(args.access): raise SystemExit(f'Banco Access não encontrado: {args.access}')
    os.makedirs(os.path.dirname(os.path.abspath(args.saida)), exist_ok=True)
    if os.path.exists(args.saida): os.remove(args.saida)
    drivers=[d for d in pyodbc.drivers() if 'Access Driver' in d]
    if not drivers: raise SystemExit('Driver Microsoft Access não encontrado.')
    conn=pyodbc.connect(f'DRIVER={{{drivers[-1]}}};DBQ={args.access};')
    out=sqlite3.connect(args.saida)
    try:
        cur_meta = conn.cursor()
        try:
            tables=[r.table_name for r in cur_meta.tables(tableType='TABLE') if r.table_name and not r.table_name.startswith('MSys')]
        finally:
            cur_meta.close()
        for table in tables:
            cols=[]
            for c in cur_meta.columns(table=table):
                cols.append((c.ordinal_position, c.column_name, c.type_name, c.data_type))
            cols.sort(key=lambda x:x[0])
            pks={r.column_name for r in cur_meta.primaryKeys(table=table)}
            defs=[]
            for _,name,typename,sqltype in cols:
                typ=TYPE_MAP.get(sqltype, 'TEXT')
                if name in pks and typ=='INTEGER':
                    defs.append(f'{q(name)} INTEGER PRIMARY KEY')
                else:
                    defs.append(f'{q(name)} {typ}')
            out.execute(f'CREATE TABLE {q(table)} ({", ".join(defs)})')
            names=[x[1] for x in cols]
            if names:
                marks=','.join('?' for _ in names)
                src=conn.cursor(); src.execute(f'SELECT * FROM {q(table)}')
                while True:
                    rows=src.fetchmany(500)
                    if not rows: break
                    vals=[[norm(v) for v in row] for row in rows]
                    out.executemany(f'INSERT INTO {q(table)} ({",".join(q(n) for n in names)}) VALUES ({marks})', vals)
            print(f'OK {table}: {out.execute(f"SELECT COUNT(*) FROM {q(table)}").fetchone()[0]} registros')
        out.commit()
    finally:
        conn.close(); out.close()
    print(f'Banco SQLite criado em: {args.saida}')

if __name__=='__main__': main()
