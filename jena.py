import re
import time

import sqlalchemy as db

from SPARQLWrapper import SPARQLWrapper, JSON
from flask import Flask, render_template, request
from sqlalchemy import text
import json

app = Flask(__name__)

db_url = 'postgres+psycopg2://postgres:8778960@localhost:5432/restaurant'
engine = db.create_engine(db_url)
connection = engine.connect()
metadata = db.MetaData()
orders = db.Table('orders', metadata, autoload=True, autoload_with=engine)
customer = db.Table('customer', metadata, autoload=True, autoload_with=engine)


@app.route('/', methods=["POST", "GET"])
def jena_sparql():
    return render_template("jena-sparql.html")


@app.route('/sparql_pg', methods=["POST", "GET"])
def fuseki_pg():
    return render_template("fuseki_pg.html")


@app.route('/sparql/do_query', methods=["POST", "GET"])
def sparql_do_query():
    sparql_input = str(request.form.get("sparql_input"))
    print('===================== request_param ============================')
    print(sparql_input)

    data = sparql_query(sparql_input)

    callback = request.values.get('callback')
    return ''.join([
        callback,
        '(',
        json.dumps(data),
        ');'
    ])


@app.route('/fuseki_pg/do_query', methods=["POST", "GET"])
def sparql_sql_do_query():
    # sparql_sql_input = str(request.form.get("sparql_sql_input"))
    start_time = int(time.time() * 1000)
    sparql_sql_input = request.values.get('sparql_sql_input').strip()
    print('===================== request_param ============================')
    print(sparql_sql_input)

    data = do_sparql_sql_query(sparql_sql_input)
    if data is not None:
        data["time"] = int(time.time() * 1000) - start_time

    callback = request.values.get('callback')
    return ''.join([
        callback,
        '(',
        json.dumps(data),
        ');'
    ])


def do_sparql_sql_query(sparql_sql_input):
    sql_time = 0
    sparql_time = 0
    sparql_sql_input = sql_format(sparql_sql_input)

    if sparql_sql_input.find("orders.") >= 0 or sparql_sql_input.find("customer.") >= 0:
        if sparql_sql_input.find("restaurant.") == -1:
            # pg 查询
            heads, sql_output_columns = get_sql_output_columns(sparql_sql_input)
            out_list, sql_time = do_sql_query(sparql_sql_input)
            return {"heads": heads, "headValues": out_list, "sqlTime": sql_time, "sparqlTime": sparql_time}

    elif sparql_sql_input.find("restaurant.") >= 0:
        # fuseki 查询
        if sparql_sql_input.lower().strip().startswith("select"):
            # sql形式
            return sql_form_sparql_query(sparql_sql_input)
        else:
            # sqarql
            return sparql_query(sparql_sql_input)

    heads, sql_output_columns = get_sql_output_columns(sparql_sql_input)

    has_fuseki_output = sql_output_columns.lower().find("restaurant") >= 0
    has_pg_output = sql_output_columns.lower().find("orders") >= 0 or sql_output_columns.lower().find("customer") >= 0
    if has_fuseki_output is False:
        # 没有fuseki的数据需要输出
        # case 1
        return first_type_sql_process_v2(heads, sparql_sql_input)
    else:
        if has_pg_output is False:
            # 只有有fuseki的数据需要输出
            # case 2
            return second_type_sql_process(heads, sparql_sql_input)
        else:
            # 输出的数据有pg也有fuseki的
            # case 5
            return five_type_sql_process(heads, sparql_sql_input)
            pass
    pass


# 取出sql中select里的列
def get_sql_output_columns(sparql_sql_input):
    select_index = sparql_sql_input.lower().find("select")
    from_index = sparql_sql_input.lower().find("from")
    sql_output_columns = sparql_sql_input[select_index + 6: from_index].strip()
    heads = sql_output_columns.replace(" ", "").split(",")
    return heads, sql_output_columns


# 输出中既有pg的列也有fuseki中的数据
def five_type_sql_process(heads, sparql_sql_input):
    # 第一步，分解出where后带指定值的 restaurant 查询
    column_value_map = parse_restaurant_query_condition(sparql_sql_input)
    fuseki_column_dict = fuseki_query_objects_v3(column_value_map)
    sparql_time = fuseki_column_dict.get("sparqlTime")
    fuseki_column_transfer_dict = column_value_transfer(fuseki_column_dict)

    # 需要输出的列: pg_columns是pg的列，fuseki_columns是restaurant中的列
    pg_columns, fuseki_columns = parse_select_column(sparql_sql_input)

    # 生成pg sql, pg_to_fuseki_column_map是辅助列，这些列是sql中带restaurant条件的pg列，pg列名对应fuseki中的列名
    sql_input, pg_to_fuseki_column_map = generate_sql_v2(fuseki_column_transfer_dict, sparql_sql_input)

    # ---------- 遍历前一步查询到的所有restaurant的数据 start -----------
    # 把辅助列对应的restaurant值组成key，指向sql需要输出的那几个restaurant列的值，可能有多条: key -> [[],[]...]
    pg_value_to_fuseki_value_map = {}
    fuseki_column_heads = fuseki_column_transfer_dict["heads"]
    for fuseki_all_column_values in fuseki_column_transfer_dict["headValues"]:
        # 辅助列对应的restaurant值
        pg_values = []
        for pg_to_fuseki_column in pg_to_fuseki_column_map.values():
            pg_to_fuseki_column = pg_to_fuseki_column[pg_to_fuseki_column.find(".") + 1:]
            index = fuseki_column_heads.index(pg_to_fuseki_column)
            pg_values.append(fuseki_all_column_values[index])

        # sql需要输出的那几个restaurant列的值
        fuseki_column_values = []
        for fuseki_column in fuseki_columns:
            index = fuseki_column_heads.index(fuseki_column[fuseki_column.find(".") + 1:])
            fuseki_column_values.append(fuseki_all_column_values[index])

        # key-value形式存储到字典中
        key = ' '.join(pg_values)
        if key in pg_value_to_fuseki_value_map:
            pg_value_to_fuseki_value_map.get(key).append(fuseki_column_values)
        else:
            tmp = [fuseki_column_values]
            pg_value_to_fuseki_value_map[key] = tmp
    # ---------- 遍历前一步查询到的所有restaurant的数据 end -----------

    # ---------- 构建sql进行pg查询 start ------------------------------
    # 辅助的列追加到pg的查询语句中，pg_extent_columns保存之前不在sql输出中的辅助列，后续需要把这些新加入的辅助列剔除
    pg_extent_columns = []
    for pg_column in pg_to_fuseki_column_map:
        if pg_column not in pg_columns:
            pg_extent_columns.append(pg_column)
    pg_columns.extend(pg_extent_columns)

    # 更改把sql中select列：移除restaurant的列，都写成 pg_columns 中的列
    sql_input = replace_select_column(sql_input, pg_columns)

    # pg 查询
    out_list, sql_time = do_sql_query(sql_input)
    # ---------- 构建sql进行pg查询 end ------------------------------

    # ---------- 需要restaurant输出列的值合并到pg的输出数据中  ----------
    out_list_tmp = []
    # 遍历pg查询到的数据
    for pg_values in out_list:
        pg_values_tmp = list(pg_values)
        pg_fuseki_column_values = []

        # 从pg查询结果中，取出每条记录的辅助列的值。（在前面我们已经把辅助列的值与对应的需要输出的restaurant列的值保存在了字典中）
        for pg_column in pg_to_fuseki_column_map:
            index = pg_columns.index(pg_column)
            value = pg_values_tmp[index]
            pg_fuseki_column_values.append(value)

        pg_values.clear()
        # 踢除辅助列的值
        for index in range(len(pg_columns)):
            new_column = pg_columns[index]
            if new_column not in pg_extent_columns:
                value = pg_values_tmp[index]
                pg_values.append(value)

        # 根据辅助列的值，找到restaurant各列的值
        key = ' '.join(pg_fuseki_column_values)
        fuseki_column_values_list = pg_value_to_fuseki_value_map[key]
        for fuseki_column_values in fuseki_column_values_list:
            pg_values_tmp = list(pg_values)
            pg_values_tmp.extend(fuseki_column_values)
            out_list_tmp.append(pg_values_tmp)

    # 输出列中先移除辅助的列
    for pg_extent_column in pg_extent_columns:
        pg_columns.remove(pg_extent_column)
    # 输出列追加restaurant列
    pg_columns.extend(fuseki_columns)

    data = {"heads": pg_columns, "headValues": out_list_tmp, "sqlTime": sql_time, "sparqlTime": sparql_time}
    return data
    pass


# 替换sql的select部分
def replace_select_column(sql_input, pg_columns):
    from_index = sql_input.lower().find("from")
    sql_from = sql_input[from_index:].strip()
    new_select = "SELECT "
    for column in pg_columns:
        new_select += column + ", "

    new_select = new_select[0:-2] + " "
    return new_select + sql_from


# 分析sql select部分哪些是pg的列，哪些是fuseki的列
def parse_select_column(sparql_sql_input):
    pg_column = []
    fuseki_column = []
    heads, sql_output_columns = get_sql_output_columns(sparql_sql_input)
    for column in sql_output_columns.split(","):
        column = column.strip()
        if column.lower().startswith("restaurant"):
            fuseki_column.append(column)
        else:
            pg_column.append(column)

    return pg_column, fuseki_column


def sql_form_sparql_query(sparql_sql_input):
    # 第一步，
    select_index = sparql_sql_input.lower().find("select")
    from_index = sparql_sql_input.lower().find("from")
    where_index = sparql_sql_input.lower().find("where")
    limit_index = sparql_sql_input.lower().find("limit")

    # sql截取
    sql_output_columns = sparql_sql_input[select_index + 6: from_index].strip()
    sql_output_columns = re.sub(r'(.*?)restaurant.(.*?)', r'\1\2', sql_output_columns)
    sql_from_str = sparql_sql_input[from_index + 4: where_index].strip()
    sql_where_str = sparql_sql_input[where_index + 5:].strip()
    sql_limit_str = ''
    if limit_index >= 0:
        sql_where_str = sparql_sql_input[where_index + 5: limit_index].strip()
        sql_limit_str = sparql_sql_input[limit_index:].strip()

    rest_ids = set([])
    where_column = ''
    matchObj = re.match(r'.*restaurant\.([a-zA-Z_]+) = ([\'\"](.*?)[\'\"]|([0-9.]+))(.*)', sparql_sql_input,
                        re.S | re.I)
    if matchObj:
        # restaurant = 'bad' 形式
        where_column = matchObj.group(1)
        column_value = matchObj.group(2).replace("'", "").replace("\"", "")
        rest_ids.add(column_value)
    else:
        # restaurant in ('bad', 'good') 形式
        matchObj = re.match(r'.*restaurant\.([a-zA-Z_]+) in \((.*?)\).*', sparql_sql_input, re.S | re.I)
        if matchObj:
            where_column = matchObj.group(1)
            rest_arrays = matchObj.group(2).split(",")
            for rest_id in rest_arrays:
                rest_ids.add(rest_id.strip()[1:-1])

    # return fuseki_query_objects(sql_output_columns.split(','), rest_ids, sql_limit_str)
    return fuseki_query_objects_v2(sql_output_columns.split(','), where_column, rest_ids, sql_limit_str)


def parse_rating_string(sql_input):
    matchObj = re.match(r'.*?ratingString = [\'\"](.*?)[\'\"].*?', sql_input, re.S | re.I)
    if matchObj:
        # print("matchObj.group(1) : " + matchObj.group(1))
        return matchObj.group(1)


# 把fuseki的查询结果列名 转成 真正的列名
def column_value_transfer(fuseki_column_dict):
    heads = fuseki_column_dict["heads"]
    headsTmp = []
    for head in heads:
        if head == 'Restaurant':
            headsTmp.append('o_rest_id')
        else:
            headsTmp.append(head.replace('Restaurant_', ''))

    fuseki_column_dict['heads'] = headsTmp
    return fuseki_column_dict


# 先 sparql 查询，再 sql 查询
def first_type_sql_process_v2(heads, sparql_sql_input):
    # 第一步，分解出where后带指定值的 restaurant 查询
    column_value_map = parse_restaurant_query_condition(sparql_sql_input)
    fuseki_column_dict = fuseki_query_objects_v3(column_value_map)
    sparql_time = fuseki_column_dict.get("sparqlTime")
    pg_column_dict = column_value_transfer(fuseki_column_dict)

    # 生成pg sql
    sql_input, pg_to_fuseki_column_map = generate_sql_v2(pg_column_dict, sparql_sql_input)
    # pg 查询
    out_list, sql_time = do_sql_query(sql_input)

    data = {"heads": heads, "headValues": out_list, "sqlTime": sql_time, "sparqlTime": sparql_time}
    return data


def parse_restaurant_query_condition(sparql_sql_input):
    key_value_dict = {}
    matchObjs = re.findall(r'.*?restaurant\.([^ ]+?) *?= *?([\'\"](.*?)[\'\"]|([0-9.]+)).*?', sparql_sql_input,
                           re.S | re.I)
    if matchObjs is not None:
        for matchObj in matchObjs:
            value = matchObj[2]
            if value is None or value == '':
                value = matchObj[3]
            key_value_dict[matchObj[0]] = value
    return key_value_dict


def parse_sql_limit(sql_limit_str):
    offset = -1
    limit = -1
    if len(sql_limit_str) > 0:
        matchObj = re.match(r'limit +(\d+).*', sql_limit_str, re.S | re.I)
        if matchObj:
            # if matchObj.group(2) is not None:
            #     offset = matchObj.group(2)
            # limit = matchObj.group(4)
            limit = matchObj.group(1)
    return limit


# 先 sql 查询，再 sparql 查询
def second_type_sql_process(heads, sparql_sql_input):
    # 第一步，
    select_index = sparql_sql_input.lower().find("select")
    from_index = sparql_sql_input.lower().find("from")
    where_index = sparql_sql_input.lower().find("where")
    limit_index = sparql_sql_input.lower().find("limit")

    # sql截取
    sql_output_columns = sparql_sql_input[select_index + 6: from_index].strip()
    sql_output_columns = re.sub(r'(.*?)restaurant.(.*?)', r'\1\2', sql_output_columns)
    sql_from_str = sparql_sql_input[from_index + 4: where_index].strip()
    sql_where_str = sparql_sql_input[where_index + 5:].strip()
    sql_limit_str = ''
    if limit_index >= 0:
        sql_where_str = sparql_sql_input[where_index + 5: limit_index].strip()
        sql_limit_str = sparql_sql_input[limit_index:].strip()

    matchObj = re.match(r'.*?orders.o_rest_id *?= *?(restaurant.o_rest_id)', sparql_sql_input, re.S | re.I)
    if matchObj:
        # orders.o_rest_id = restaurant.o_rest_id 格式
        columns = sql_from_str.split(',')
        sql_from_str = ''
        for i in range(0, len(columns)):
            if columns[i].strip() != 'restaurant':
                sql_from_str += columns[i].strip()
                if i < len(columns) - 1:
                    sql_from_str += ' ,'
        if sql_from_str.endswith(","):
            sql_from_str = sql_from_str[0:-1]

        sql_where_str = re.sub(r'(.*)orders.o_rest_id *?= *?restaurant.o_rest_id(.*)', r'\1 1=1 \2', sql_where_str)
        # sql 查询
        rest_id_arrays, sql_time = sql_query_object(sql_from_str, sql_where_str)
        rest_ids = set([])
        for rest_id_array in rest_id_arrays:
            for rest_id in rest_id_array:
                rest_ids.add(rest_id)

        # fuseki 查询
        data = fuseki_query_objects_v2(sql_output_columns.split(','), 'o_rest_id', rest_ids, sql_limit_str)
        data["sqlTime"] = sql_time
        return data

    elif re.match(r'.*?restaurant.o_rest_id *?in.*?', sparql_sql_input, re.S | re.I):
        matchObj = re.match(r'.*?restaurant.o_rest_id *?in \((.*?)\).*?', sparql_sql_input, re.S | re.I)
        if matchObj:
            sql_query = matchObj.group(1)
            # sql 查询
            rest_id_arrays, sql_time = do_sql_query(sql_query)
            rest_ids = set([])
            for rest_id_array in rest_id_arrays:
                for rest_id in rest_id_array:
                    rest_ids.add(rest_id)

            # fuseki 查询
            data = fuseki_query_objects_v2(sql_output_columns.split(','), 'o_rest_id', rest_ids, sql_limit_str)
            data["sqlTime"] = sql_time
            return data


def sql_query_object(sql_from_str, sql_where_str):
    sql = 'SELECT orders.o_rest_id \n' + \
          ' From ' + sql_from_str + "\n" + \
          ' Where ' + sql_where_str
    return do_sql_query(sql)


def column_in_value(column, pg_column_dict):
    index = -1
    values = set([])
    heads = pg_column_dict['heads']
    for i in range(0, len(heads)):
        if heads[i] == column:
            index = i
            break

    if index >= 0:
        head_values = pg_column_dict['headValues']
        for head_value in head_values:
            values.add(head_value[index])

    if len(values) > 0:
        return ' in (' + str(values)[1:-1] + ')'
    else:
        return ' in ( )'


def generate_sql_v2(pg_column_dict, sparql_sql_input):
    # 剔除 restaurant.ratingString = *** 的条件
    matchObj = re.match(r'(.*?)restaurant\.([^ ]+?) = ([\'\"](.*?)[\'\"]|([0-9.]+?))(.*)', sparql_sql_input,
                        re.S | re.I)
    sql_input_tmp = matchObj.group(1) + '1=1' + matchObj.group(6)

    equal_seq = re.compile(r'= *?restaurant\.[^ ]+?').findall(sql_input_tmp)
    # print(equal_seq)
    pg_to_fuseki_column_map = {}
    if len(equal_seq) > 0:
        # case 1: orders.o_rest_id = restaurant.o_rest_id
        for column in pg_column_dict['heads']:
            # sql_input_tmp = sql_input_tmp
            where_index = sparql_sql_input.lower().find("where")

            # sql截取
            sql_where = sparql_sql_input[where_index + 6:].strip()
            if sql_where.find(column):
                # matchObj = re.match(r'(.*[^\w]where[^\w].* (.+?))= *?restaurant\.' + column + '(.*)', sql_input_tmp, re.S | re.I)
                # matchObj = re.match(r'(.*[^\w](.+?))= *?restaurant\.' + column + '(.*)', sql_input_tmp, re.S | re.I)
                matchObj = re.match(r'(.*[^\w\.]([^ ]+)) = (restaurant)\.' + column + '(.*)', sql_input_tmp,
                                    re.S | re.I)
                if matchObj:
                    pg_column = matchObj.group(2)
                    fuseki_column = matchObj.group(3) + '.' + column
                    sql_input_tmp = matchObj.group(1) + column_in_value(column, pg_column_dict) + matchObj.group(4)
                    pg_to_fuseki_column_map[pg_column.strip()] = fuseki_column.strip()
    else:
        # case 2: orders.o_rest_id in (...)
        equal_seq = re.compile(r' in \(.*restaurant\..*?\)').findall(sql_input_tmp)
        if len(equal_seq) > 0:
            matchObj = re.match(r'(.* (.+?)) in \(.*?(restaurant)\.(.+?) .*?\)(.*)', sql_input_tmp, re.S | re.I)
            pg_column = matchObj.group(2)
            fuseki_column = matchObj.group(3) + '.' + matchObj.group(4)
            sql_input_tmp = matchObj.group(1) + column_in_value(matchObj.group(4), pg_column_dict) + matchObj.group(54)
            pg_to_fuseki_column_map[pg_column.strip()] = fuseki_column.strip()

    # remove 'restaurant' in sql
    matchObj = re.match(r'(.*)(?:,) *?restaurant(.*)', sql_input_tmp, re.S | re.I)
    if matchObj:
        sql_input_tmp = matchObj.group(1) + matchObj.group(2)

    return sql_input_tmp, pg_to_fuseki_column_map


def do_sql_query(sql_input_tmp):
    start_time = int(time.time() * 1000)

    print('===================== sql_query ============================')
    print(sql_input_tmp)
    sql = text(sql_input_tmp)
    ResultProxy = connection.execute(sql)
    ResultSet = ResultProxy.fetchall()
    out_list = []
    for r in ResultSet:
        out_list.append(list(r))

    return out_list, (int(time.time() * 1000) - start_time)


def fuseki_query_objects_v3(column_value_dict):
    start_time = int(time.time() * 1000)
    select_columns = ['label', 'rating', 'ratingString ', 'foodType', 'location']
    sparql_input_tpl = """
    prefix mooney: <http://www.mooney.net/restaurant#>
    SELECT  ?Restaurant  ?Restaurant_ratingString ?Restaurant_label ?Restaurant_rating ?Restaurant_foodType ?Restaurant_location
    WHERE {}
      ?Restaurant mooney:ratingString ?Restaurant_ratingString .
      ?Restaurant mooney:label ?Restaurant_label .
      ?Restaurant mooney:rating ?Restaurant_rating .
      ?Restaurant mooney:foodType ?Restaurant_foodType .
      ?Restaurant mooney:location ?Restaurant_location .

{}
    {}
    """

    filter_sql = ''
    for column, value in column_value_dict.items():
        filter_sql_tmp = ''
        if column == 'o_rest_id':
            filter_sql_tmp += ' ?Restaurant = <http://www.mooney.net/restaurant#{}> ||'.format(value)
        elif column == 'rating' or value.replace(".", '').isdigit():
            filter_sql_tmp += ' ?Restaurant_{} = {} ||'.format(column, value)
        else:
            filter_sql_tmp += ' ?Restaurant_{} = \'{}\' ||'.format(column, value)

        filter_sql_tmp = filter_sql_tmp[0:-2]
        filter_sql_tmp = '      FILTER ({})'.format(filter_sql_tmp)

        filter_sql += filter_sql_tmp + '\n'

    sparql_input = sparql_input_tpl.format('{', filter_sql, '}')

    data = sparql_query(sparql_input)
    heads = data.get("heads")
    data_list = []
    for values in data.get('headValues'):
        list_tmp = []
        for valueTmp in values:
            list_tmp.append(valueTmp.replace('http://www.mooney.net/restaurant#', ''))
        data_list.append(list_tmp)

    data = {"heads": heads, "headValues": data_list, "sparqlTime": (int(time.time() * 1000) - start_time)}

    return data


def fuseki_query_objects_v2(select_columns, filter_column, filter_values, sql_limit_str):
    start_time = int(time.time() * 1000)

    global out_put_column
    sparql_input_tpl = """
    prefix mooney: <http://www.mooney.net/restaurant#>
    SELECT {}
    WHERE {}
{}
{}
    {}
    """
    out_put_columns = set(select_columns)
    out_put_sql = ''
    select_sql = '?Restaurant '

    for out_put_column in out_put_columns:
        if out_put_column != 'o_rest_id':
            select_sql += " ?Restaurant_{}".format(out_put_column.strip())

    out_put_columns.add(filter_column)
    for out_put_column in out_put_columns:
        if out_put_column != 'o_rest_id':
            out_put_sql += "      ?Restaurant mooney:{} ?Restaurant_{} .\n".format(out_put_column.strip(),
                                                                                   out_put_column.strip())
    out_put_sql = out_put_sql.rstrip()

    heads = ''
    data_list = []

    sql_limit = int(parse_sql_limit(sql_limit_str))

    limit = 20
    start = 0
    filter_values_list = list(filter_values)
    while start < len(filter_values_list):
        end = start + limit
        if end >= len(filter_values_list):
            end = len(filter_values_list)
        filter_values_sub = filter_values_list[start: end]
        start = end

        filter_sql = ''
        for filter_value in filter_values_sub:
            if filter_column == 'o_rest_id':
                filter_sql += ' ?Restaurant = <http://www.mooney.net/restaurant#{}> ||'.format(filter_value)
            elif filter_column == 'rating' or filter_value.replace(".", '').isdigit():
                filter_sql += ' ?Restaurant_{} = {} ||'.format(filter_column, filter_value)
            else:
                filter_sql += ' ?Restaurant_{} = \'{}\' ||'.format(filter_column, filter_value)

        filter_sql = filter_sql[0:-2]
        filter_sql = '      FILTER ({})'.format(filter_sql)

        sparql_input = sparql_input_tpl.format(select_sql, '{', out_put_sql, filter_sql, '}')
        sparql_input += sql_limit_str.strip()
        data = sparql_query(sparql_input)

        heads = data.get("heads")
        for values in data.get('headValues'):
            list_tmp = []
            for valueTmp in values:
                list_tmp.append(valueTmp.replace('http://www.mooney.net/restaurant#', ''))
            data_list.append(list_tmp)

        if 0 < sql_limit < len(data_list):
            data_list = data_list[0:sql_limit]
            break

    data = {"heads": heads, "headValues": data_list, "sparqlTime": (int(time.time() * 1000) - start_time)}

    return data


def sparql_query(sparql_input):
    start_time = int(time.time() * 1000)
    sparql_input = sparql_input.strip()
    print('===================== sparql_query ============================')
    print(sparql_input)

    if sparql_input.endswith(";"):
        sparql_input = sparql_input[0:-1]

    sparql = SPARQLWrapper("http://localhost:8080/fuseki/Restaurant")
    sparql.setQuery(sparql_input)
    sparql.setReturnFormat(JSON)
    results = sparql.query().convert()

    # print(results)
    data = data_format(results, start_time)
    # print(data)
    return data


# sql格式化一下
def sql_format(sql_input):
    # 等号左右加空格
    if sql_input.find("=") >= 0:
        sql_input = sql_input.replace("=", " = ")

    # 逗号右加空格
    if sql_input.find(",") >= 0:
        sql_input = sql_input.replace(",", ", ")

    # 去掉所有双空格的情况
    while sql_input.find("  ") >= 0:
        sql_input = sql_input.replace("  ", " ")

    # 句号后不能带空格
    while sql_input.find(". ") >= 0:
        sql_input = sql_input.replace(". ", ".")

    if sql_input.find(" in(") >= 0:
        sql_input = sql_input.replace(" in(", " in (")

    return sql_input


def data_format(results, start_time):
    heads = []
    headValues = []
    for head in results["head"]["vars"]:
        heads.append(head)

    for result in results["results"]["bindings"]:
        values = []
        for head in heads:
            values.append(result[head]["value"])
        headValues.append(values)

    data = {"heads": heads, "headValues": headValues, "sparqlTime": (int(time.time() * 1000) - start_time)}
    return data


def test1():
    sql_input = """
    SELECT orders.o_orderkey, orders.o_orderstatus, customer.c_name
    FROM orders, customer, restaurant
    WHERE orders.o_custkey = customer.c_custkey AND orders.o_rest_id = restaurant.o_rest_id AND restaurant.ratingString = 'bad';
    """
    first_type_sql_process_v2(None, sql_input)


def test2():
    sql_input = """
    SELECT orders.o_orderkey, orders.o_orderstatus
    FROM orders, restaurant
    WHERE orders.o_rest_id in (SELECT restaurant.o_rest_id FROM restaurant WHERE restaurant.ratingString = 'bad');
    """
    parse_restaurant_query_condition(sql_input)


if __name__ == '__main__':
    # test1()
    # test2()
    app.run(port=5100)
