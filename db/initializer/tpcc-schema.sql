CREATE TABLE IF NOT EXISTS warehouse (
    w_id smallint NOT NULL PRIMARY KEY,
    w_name varchar(10) NOT NULL,
    w_street_1 varchar(20) NOT NULL,
    w_street_2 varchar(20) NOT NULL,
    w_city varchar(20) NOT NULL,
    w_state char(2) NOT NULL,
    w_zip char(9) NOT NULL,
    w_tax numeric(4, 4) NOT NULL,
    w_ytd numeric(12, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS district (
    d_id smallint NOT NULL,
    d_w_id smallint NOT NULL REFERENCES warehouse (w_id),
    d_name varchar(10) NOT NULL,
    d_street_1 varchar(20) NOT NULL,
    d_street_2 varchar(20) NOT NULL,
    d_city varchar(20) NOT NULL,
    d_state char(2) NOT NULL,
    d_zip char(9) NOT NULL,
    d_tax numeric(4, 4) NOT NULL,
    d_ytd numeric(12, 2) NOT NULL,
    d_next_o_id integer NOT NULL,
    PRIMARY KEY (d_w_id, d_id)
);

CREATE TABLE IF NOT EXISTS item (
    i_id integer NOT NULL PRIMARY KEY,
    i_im_id integer NOT NULL,
    i_name varchar(24) NOT NULL,
    i_price numeric(5, 2) NOT NULL,
    i_data varchar(50) NOT NULL
);

CREATE TABLE IF NOT EXISTS stock (
    s_w_id smallint NOT NULL REFERENCES warehouse (w_id),
    s_i_id integer NOT NULL REFERENCES item (i_id),
    s_quantity smallint NOT NULL,
    s_dist_01 char(24) NOT NULL,
    s_dist_02 char(24) NOT NULL,
    s_dist_03 char(24) NOT NULL,
    s_dist_04 char(24) NOT NULL,
    s_dist_05 char(24) NOT NULL,
    s_dist_06 char(24) NOT NULL,
    s_dist_07 char(24) NOT NULL,
    s_dist_08 char(24) NOT NULL,
    s_dist_09 char(24) NOT NULL,
    s_dist_10 char(24) NOT NULL,
    s_ytd numeric(8, 2) NOT NULL,
    s_order_cnt smallint NOT NULL,
    s_remote_cnt smallint NOT NULL,
    s_data varchar(50) NOT NULL,
    PRIMARY KEY (s_w_id, s_i_id)
);

CREATE TABLE IF NOT EXISTS customer (
    c_id smallint NOT NULL,
    c_d_id smallint NOT NULL,
    c_w_id smallint NOT NULL,
    c_first varchar(16) NOT NULL,
    c_middle char(2) NOT NULL,
    c_last varchar(16) NOT NULL,
    c_street_1 varchar(20) NOT NULL,
    c_street_2 varchar(20) NOT NULL,
    c_city varchar(20) NOT NULL,
    c_state char(2) NOT NULL,
    c_zip char(9) NOT NULL,
    c_phone char(16) NOT NULL,
    c_since timestamp NOT NULL,
    c_credit char(2) NOT NULL,
    c_credit_lim numeric(12, 2) NOT NULL,
    c_discount numeric(4, 4) NOT NULL,
    c_balance numeric(12, 2) NOT NULL,
    c_ytd_payment numeric(12, 2) NOT NULL,
    c_payment_cnt smallint NOT NULL,
    c_delivery_cnt smallint NOT NULL,
    c_data varchar(500) NOT NULL,
    PRIMARY KEY (c_w_id, c_d_id, c_id),
    FOREIGN KEY (c_w_id, c_d_id) REFERENCES district (d_w_id, d_id)
);

CREATE TABLE IF NOT EXISTS history (
    h_c_id smallint NOT NULL,
    h_c_d_id smallint NOT NULL,
    h_c_w_id smallint NOT NULL,
    h_d_id smallint NOT NULL,
    h_w_id smallint NOT NULL,
    h_date timestamp NOT NULL,
    h_amount numeric(6, 2) NOT NULL,
    h_data varchar(24) NOT NULL,
    FOREIGN KEY (h_c_w_id, h_c_d_id, h_c_id) REFERENCES customer (c_w_id, c_d_id, c_id),
    FOREIGN KEY (h_w_id) REFERENCES warehouse (w_id),
    FOREIGN KEY (h_w_id, h_d_id) REFERENCES district (d_w_id, d_id)
);

CREATE TABLE IF NOT EXISTS oorder (
    o_id integer NOT NULL,
    o_d_id smallint NOT NULL,
    o_w_id smallint NOT NULL,
    o_c_id smallint NOT NULL,
    o_entry_d timestamp NOT NULL,
    o_carrier_id smallint,
    o_ol_cnt smallint NOT NULL,
    o_all_local smallint NOT NULL,
    PRIMARY KEY (o_w_id, o_d_id, o_id),
    FOREIGN KEY (o_w_id) REFERENCES warehouse (w_id),
    FOREIGN KEY (o_w_id, o_d_id) REFERENCES district (d_w_id, d_id),
    FOREIGN KEY (o_w_id, o_d_id, o_c_id) REFERENCES customer (c_w_id, c_d_id, c_id)
);

CREATE INDEX IF NOT EXISTS idx_oorder_customer ON oorder (o_w_id, o_d_id, o_c_id);

CREATE TABLE IF NOT EXISTS new_order (
    no_w_id smallint NOT NULL,
    no_d_id smallint NOT NULL,
    no_o_id integer NOT NULL,
    PRIMARY KEY (no_w_id, no_d_id, no_o_id),
    FOREIGN KEY (no_w_id, no_d_id, no_o_id) REFERENCES oorder (o_w_id, o_d_id, o_id)
);

CREATE TABLE IF NOT EXISTS order_line (
    ol_w_id smallint NOT NULL,
    ol_d_id smallint NOT NULL,
    ol_o_id integer NOT NULL,
    ol_number smallint NOT NULL,
    ol_i_id integer NOT NULL,
    ol_supply_w_id smallint NOT NULL,
    ol_delivery_d timestamp,
    ol_quantity smallint NOT NULL,
    ol_amount numeric(6, 2) NOT NULL,
    ol_dist_info char(24) NOT NULL,
    PRIMARY KEY (ol_w_id, ol_d_id, ol_o_id, ol_number),
    FOREIGN KEY (ol_w_id, ol_d_id, ol_o_id) REFERENCES oorder (o_w_id, o_d_id, o_id),
    FOREIGN KEY (ol_i_id) REFERENCES item (i_id),
    FOREIGN KEY (ol_supply_w_id) REFERENCES warehouse (w_id),
    FOREIGN KEY (ol_w_id, ol_i_id) REFERENCES stock (s_w_id, s_i_id)
);

CREATE INDEX IF NOT EXISTS idx_order_line_item ON order_line (ol_i_id);
