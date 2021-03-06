#!/usr/bin/env python3

import pywind.evtframework.handler.tcp_handler as tcp_handler
import pywind.web.lib.wsgi as wsgi
import socket


class scgiErr(Exception): pass


class scgid_listen(tcp_handler.tcp_handler):
    # 最大连接数
    __max_conns = 0
    __current_conns = 0
    __configs = None
    __wsgi = None

    def init_func(self, creator_fd, configs):
        self.__configs = configs
        self.__max_conns = configs.get("max_conns", 10)
        s = socket.socket()
        self.set_socket(s)
        listen = configs.get("listen", ("127.0.0.1", 8000,))

        self.bind(listen)
        return self.fileno

    def after(self):
        self.listen(10)
        self.register(self.fileno)
        self.add_evt_read(self.fileno)

    def tcp_accept(self):
        while 1:
            try:
                cs, caddr = self.accept()
            except BlockingIOError:
                break
            if self.__current_conns == self.__max_conns:
                cs.close()
                continue
            self.create_handler(self.fileno, scgid, cs, caddr, self.__configs)
            self.__current_conns += 1

    def handler_ctl(self, from_fd, cmd, *args, **kwargs):
        if cmd != "close_conn": return
        self.__current_conns -= 1


class scgid(tcp_handler.tcp_handler):
    __creator = -1
    __application = None
    __timeout = 0
    __header_ok = False
    __wsgi = None

    def __parse_scgi_header(self):
        size = self.reader.size()
        rdata = self.reader.read()
        data_bak = rdata
        pos = rdata.find(b":")
        t = pos + 1

        if pos < 0 and size > 16: raise scgiErr("cannot found length")
        try:
            tot_len = int(rdata[0:pos])
        except ValueError:
            raise scgiErr("invalid length character")
        pos += 1
        rdata = rdata[pos:]
        if rdata[0:14] != b"CONTENT_LENGTH": raise scgiErr("cannot found content_length at first")
        rdata = rdata[15:]
        pos = rdata.find(b"\0")
        if pos < 0: raise scgiErr("cannot found content_length border")

        try:
            content_length = int(rdata[0:pos])
        except ValueError:
            raise scgiErr("invalid content_length character")

        pos += 1
        rdata = rdata[pos:]
        hdr_size = tot_len - content_length

        if (len(data_bak[t:])) < hdr_size + 1: return (False, None, None,)

        if rdata[-1] != ord(","): raise scgiErr("cannot found scgi character ',' ")

        sts = rdata[0:-2].decode("iso-8859-1")
        tmplist = sts.split("\0")
        Lsize = len(tmplist)
        if Lsize % 2 != 0: raise scgiErr("invalid scgi header")
        cgi_env = {}
        a, b = (0, 1,)

        while b < Lsize:
            name = tmplist[a]
            value = tmplist[b]
            cgi_env[name] = value
            a = a + 2
            b = b + 2

        cgi_env["CONTENT_LENGTH"] = content_length
        t = t + hdr_size + 1

        return (True, cgi_env, data_bak[t:],)

    def init_func(self, creator_fd, cs, caddr, configs):
        self.__creator = creator_fd
        self.__application = configs.get("application", None)
        self.__timeout = configs.get("timeout", 30)
        self.set_socket(cs)
        self.register(self.fileno)
        self.add_evt_read(self.fileno)

        return self.fileno

    def tcp_readable(self):
        if self.__header_ok:
            self.__wsgi.input(self.reader.read())
            return
        ok, cgi_env, body_data = self.__parse_scgi_header()
        if not ok: return
        self.__header_ok = True

        del cgi_env["SCGI"]

        self.__wsgi = wsgi.wsgi(self.__application, cgi_env,
                                self.__resp_header,
                                self.__resp_body_data,
                                self.__finish_request)
        self.__wsgi.input(body_data)
        self.add_evt_write(self.fileno)

    def tcp_writable(self):
        self.__wsgi.handle()

    def tcp_error(self):
        self.delete_handler(self.fileno)

    def tcp_timeout(self):
        self.delete_handler(self.fileno)

    def tcp_delete(self):
        self.unregister(self.fileno)
        self.close()
        self.ctl_handler(self.fileno, self.__creator, "close_conn")

        if self.__wsgi: self.__wsgi.finish()

    def __finish_request(self):
        self.delete_this_no_sent_data()

    def __resp_body_data(self, body_data):
        self.writer.write(body_data)

    def __resp_header(self, status, resp_headers):
        tmplist = ["Status: %s\r\n" % status, ]

        for name, value in resp_headers:
            sts = "%s: %s\r\n" % (name, value, )
            tmplist.append(sts)
        tmplist.append("\r\n")

        self.writer.write("".join(tmplist).encode())
