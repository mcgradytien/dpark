from __future__ import absolute_import
import os
import sys
from six.moves import map
from six.moves import range
from six.moves import zip
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bz2
import gzip
import unittest
import random
import operator
import shutil
import logging
import binascii
import tempfile
import contextlib
from dpark.context import *
from dpark.rdd import *
from dpark.beansdb import is_valid_key, restore_value
from dpark.accumulator import *
from tempfile import mkdtemp
from dpark.serialize import loads, dumps

logging.getLogger('dpark').setLevel(logging.ERROR)


@contextlib.contextmanager
def gen_big_text_file(block_size, file_size, ext='txt'):
    if not ext.startswith('.'):
        ext = '.' + ext

    cnt = 0
    with tempfile.NamedTemporaryFile(suffix=ext) as out:
        while out.tell() < file_size:
            with tempfile.NamedTemporaryFile() as tmp:
                if ext == '.bz2':
                    f = bz2.BZ2File(tmp.name, 'wb')
                elif ext == '.gz':
                    f = gzip.GzipFile(tmp.name, 'wb')
                else:
                    f = open(tmp.name, 'w+b')

                with contextlib.closing(f):
                    while True:
                        size = random.randint(0, 512)
                        line = binascii.b2a_base64(os.urandom(size))
                        f.write(line)
                        cnt += 1
                        if f.tell() > block_size:
                            break

                while True:
                    r = tmp.read(4 << 20)
                    if not r:
                        break
                    out.write(r)

                out.flush()

        out.cnt = cnt
        yield out


class TestRDD(unittest.TestCase):
    def setUp(self):
        self.sc = DparkContext("local")

    def tearDown(self):
        self.sc.stop()

    def test_parallel_collection(self):
        slices = ParallelCollection.slice(range(5), 3)
        self.assertEqual(len(slices), 3)
        self.assertEqual(list(slices[0]), list(range(2)))
        self.assertEqual(list(slices[1]), list(range(2, 4)))
        self.assertEqual(list(slices[2]), list(range(4, 5)))

    def test_basic_operation(self):
        d = list(range(4))
        nums = self.sc.makeRDD(d, 2)
        self.assertEqual(len(nums.splits), 2)
        self.assertEqual(nums.collect(), d)
        self.assertEqual(nums.reduce(lambda x,y:x+y), sum(d))
        self.assertEqual(nums.map(lambda x:str(x)).collect(), ["0", "1", "2", "3"])
        self.assertEqual(nums.filter(lambda x:x>1).collect(), [2, 3])
        self.assertEqual(nums.flatMap(lambda x:list(range(x))).collect(), [0, 0,1, 0,1,2])
        self.assertEqual(nums.union(nums).collect(), d + d)
        self.assertEqual(nums.cartesian(nums).map(lambda x_y:x_y[0]*x_y[1]).reduce(lambda x,y:x+y), 36)
        self.assertEqual(nums.glom().map(lambda x:list(x)).collect(),[[0,1],[2,3]])
        self.assertEqual(nums.mapPartitions(lambda x:[sum(x)]).collect(),[1, 5])
        self.assertEqual(nums.map(lambda x:str(x)+"/").reduce(lambda x,y:x+y),
            "0/1/2/3/")
        self.assertEqual(nums.pipe('grep 3').collect(), [b'3'])
        self.assertEqual(nums.sample(0.5, True).count(), 2)

        self.assertEqual(len(nums[:1]), 1)
        self.assertEqual(nums[:1].collect(), list(range(2)))
        self.assertEqual(len(nums.mergeSplit(2)), 1)
        self.assertEqual(nums.mergeSplit(2).collect(), list(range(4)))
        self.assertEqual(nums.zipWith(nums).collectAsMap(), dict(list(zip(d,d))))

    def test_ignore_bad_record(self):
        d = list(range(100))
        self.sc.options.err = 0.02
        nums = self.sc.makeRDD(d, 2)
        self.assertEqual(nums.filter(lambda x:1.0/x).count(), 99)
        self.assertEqual(nums.map(lambda x:1//x).count(), 99)
        self.assertEqual(nums.flatMap(lambda x:[1//x]).count(), 99)
        self.assertEqual(nums.reduce(lambda x,y:x+100//y), 431)

    def test_pair_operation(self):
        d = list(zip([1,2,3,3], list(range(4,8))))
        nums = self.sc.makeRDD(d, 2)
        self.assertEqual(nums.reduceByKey(lambda x,y:x+y).collectAsMap(), {1:4, 2:5, 3:13})
        self.assertEqual(nums.reduceByKeyToDriver(lambda x,y:x+y), {1:4, 2:5, 3:13})
        self.assertEqual(nums.groupByKey().collectAsMap(), {1:[4], 2:[5], 3:[6,7]})

        # join
        nums2 = self.sc.makeRDD(list(zip([2,3,4], [1,2,3])), 2)
        self.assertEqual(nums.join(nums2).collect(),
                [(2, (5, 1)), (3, (6, 2)), (3, (7, 2))])
        self.assertEqual(sorted(nums.leftOuterJoin(nums2).collect()),
                [(1, (4,None)), (2, (5, 1)), (3, (6, 2)), (3, (7, 2))])
        self.assertEqual(sorted(nums.rightOuterJoin(nums2).collect()),
                [(2, (5,1)), (3, (6,2)), (3, (7,2)), (4,(None,3))])
        self.assertEqual(nums.innerJoin(nums2).collect(),
                [(2, (5, 1)), (3, (6, 2)), (3, (7, 2))])

        # join - data contains duplicate key
        numsDup = self.sc.makeRDD(list(zip([2,2,4], [1,2,3])), 2)
        self.assertEqual(nums.join(numsDup).collect(),
                [(2, (5, 1)), (2, (5, 2))])
        self.assertEqual(nums.innerJoin(numsDup).collect(),
                [(2, (5, 1)), (2, (5, 2))])

        self.assertEqual(nums.mapValue(lambda x:x+1).collect(),
                [(1, 5), (2, 6), (3, 7), (3, 8)])
        self.assertEqual(nums.flatMapValue(lambda x:list(range(x))).count(), 22)
        self.assertEqual(nums.groupByKey().lookup(3), [6,7])
        self.assertEqual(nums.partitionByKey().lookup(2), 5)
        self.assertEqual(nums.partitionByKey().lookup(4), None)
        self.assertEqual(nums.lookup(2), 5)
        self.assertEqual(nums.lookup(4), None)

        # group with
        self.assertEqual(sorted(nums.groupWith(nums2).collect()),
                [(1, ([4],[])), (2, ([5],[1])), (3,([6,7],[2])), (4,([],[3]))])
        nums3 = self.sc.makeRDD(list(zip([4,5,1], [1,2,3])), 1).groupByKey(2).flatMapValue(lambda x:x)
        self.assertEqual(sorted(nums.groupWith([nums2, nums3]).collect()),
                [(1, ([4],[],[3])), (2, ([5],[1],[])), (3,([6,7],[2],[])),
                (4,([],[3],[1])), (5,([],[],[2]))])

        # group with top n per group
        ks = [1, 2, 2, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 5, 6, 6, 6, 6, 6, 6]
        ds = list(zip(ks, list(range(5, 26))))
        nums4 = self.sc.makeRDD(ds, 2)
        self.assertEqual(nums4.topByKey(top_n=2).lookup(3),
                         [8, 9])
        self.assertEqual(nums4.topByKey(top_n=3, reverse=True).lookup(4),
                         [14, 13, 12])

        # test stable order

        l = [(3, (1, 0)), (3, (2, 1)), (2, (2, 2)), (3, (3, 3)), (2, (2, 4)),
             (1, (1, 5)), (3, (3, 6)), (1, (2, 7)), (1, (3, 8)), (3, (2, 9)),
             (3, (1, 10)), (2, (2, 11)), (1, (3, 12)), (2, (2, 13)), (3, (1, 14)),
             (2, (2, 15)), (1, (2, 16)), (3, (3, 17)), (1, (1, 18)), (2, (2, 19)),
             (2, (3, 20)), (3, (1, 21)), (1, (2, 22)), (3, (2, 23)), (2, (2, 24)),
             (2, (2, 25)), (2, (2, 26)), (3, (1, 27)), (2, (3, 28)), (1, (3, 29))]
        nums5 = self.sc.makeRDD(l, 2)
        val_rev = [(3, 8), (3, 12), (3, 29)]
        val = [(1, 0), (1, 10)]
        self.assertEqual(nums5.topByKey(top_n=3, reverse=True, order_func=lambda x: x[0]).lookup(1), val_rev)
        self.assertEqual(nums5.topByKey(top_n=2, order_func=lambda x: x[0]).lookup(3), val)


        # update
        rdd4 = self.sc.makeRDD([('foo', 1), ('wtf', 233)])
        rdd5 = self.sc.makeRDD([('foo', 2), ('bar', 3), ('wtf', None)])
        rdd6 = self.sc.makeRDD([('dup', 1), ('dup', 2), ('duq', 3), ('duq', 4),
                                ('foo', 5)])
        rdd7 = self.sc.makeRDD([('duq', 6), ('duq', 7), ('duq', 8), ('dup', 9),
                                ('bar', 10)])
        dct = rdd6.update(rdd7).collectAsMap()
        dct2 = rdd7.update(rdd6).collectAsMap()

        self.assertEqual(
            rdd4.update(rdd5, replace_only=True).collectAsMap(),
            dict([('foo', 2), ('wtf', None)])
        )
        self.assertEqual(
            rdd5.update(rdd4, replace_only=True).collectAsMap(),
            dict([('foo', 1), ('bar', 3), ('wtf', 233)])
        )
        self.assertEqual(
            rdd4.update(rdd5).collectAsMap(),
            dict([('foo', 2), ('bar', 3), ('wtf', None)])
        )
        self.assertEqual(
            rdd5.update(rdd4).collectAsMap(),
            dict([('foo', 1), ('bar', 3), ('wtf', 233)])
        )
        self.assertEqual(dct.get('dup'), 9)
        self.assertEqual(dct.get('foo'), 5)
        self.assertTrue(dct.get('duq') in {6, 7, 8})
        self.assertEqual(dct.get('bar'), 10)
        self.assertTrue(dct2.get('dup') in {1, 2})
        self.assertEqual(dct2.get('foo'), 5)
        self.assertTrue(dct2.get('duq') in {3, 4})
        self.assertEqual(dct2.get('bar'), 10)

    def test_accumulater(self):
        d = list(range(4))
        nums = self.sc.makeRDD(d, 2)

        acc = self.sc.accumulator()
        nums.map(lambda x: acc.add(x)).count()
        self.assertEqual(acc.value, 6)

        acc = self.sc.accumulator([], listAcc)
        nums.map(lambda x: acc.add([x])).count()
        self.assertEqual(list(sorted(acc.value)), list(range(4)))

    def test_sort(self):
        d = list(range(100))
        self.assertEqual(self.sc.makeRDD(d, 10).collect(), list(range(100)))
        random.shuffle(d)
        rdd = self.sc.makeRDD(d, 10)
        self.assertEqual(rdd.sort(numSplits=10).collect(), list(range(100)))
        self.assertEqual(rdd.sort(reverse=True, numSplits=5).collect(), list(reversed(list(range(100)))))
        self.assertEqual(rdd.sort(key=lambda x:-x, reverse=True, numSplits=4).collect(), list(range(100)))

        self.assertEqual(rdd.top(), list(range(90, 100))[::-1])
        self.assertEqual(rdd.top(15, lambda x:-x), list(range(0, 15)))

        for i in range(10):
            for j in range(i+1):
                d.append(i)
        rdd = self.sc.makeRDD(d, 10)
        self.assertEqual(rdd.hot(), list(zip(list(range(9, -1, -1)), list(range(11, 1, -1)))))

    def test_empty_rdd(self):
        rdd = self.sc.union([])
        self.assertEqual(rdd.count(), 0)
        self.assertEqual(rdd.sort().collect(), [])

    def test_text_file(self):
        path = 'tests/test_rdd.py'
        f = self.sc.textFile(path, splitSize=1000).mergeSplit(numSplits=1)
        with open(path) as f_:
            n = len(f_.read().split())

        fs = f.flatMap(lambda x:x.split()).cache()
        self.assertEqual(fs.count(), n)
        self.assertEqual(fs.map(lambda x:(x,1)).reduceByKey(lambda x,y: x+y).collectAsMap()['class'], 1)
        prefix = 'prefix:'
        self.assertEqual(f.map(lambda x:prefix+x).saveAsTextFile('/tmp/tout'),
            ['/tmp/tout/0000'])
        self.assertEqual(f.map(lambda x:('test', prefix+x)).saveAsTextFileByKey('/tmp/tout'),
            ['/tmp/tout/test/0000'])
        d = self.sc.textFile('/tmp/tout')
        with open(path) as f:
            n = len(f.readlines())

        self.assertEqual(d.count(), n)
        self.assertEqual(fs.map(lambda x:(x,1)).reduceByKey(operator.add
            ).saveAsCSVFile('/tmp/tout'),
            ['/tmp/tout/0000.csv'])
        shutil.rmtree('/tmp/tout')

    def test_compressed_file(self):
        # compress
        d = self.sc.makeRDD(list(range(100000)), 1)
        self.assertEqual(d.map(str).saveAsTextFile('/tmp/tout', compress=True),
            ['/tmp/tout/0000.gz'])
        rd = self.sc.textFile('/tmp/tout', splitSize=10<<10)
        self.assertEqual(rd.count(), 100000)

        self.assertEqual(d.map(lambda i:('x', str(i))).saveAsTextFileByKey('/tmp/tout', compress=True),
            ['/tmp/tout/x/0000.gz'])
        rd = self.sc.textFile('/tmp/tout', splitSize=10<<10)
        self.assertEqual(rd.count(), 100000)
        shutil.rmtree('/tmp/tout')

    def test_large_txt_file(self):
        with gen_big_text_file(64 << 10, 5 << 20, ext='txt') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)

        with gen_big_text_file(1 << 20, 5 << 20, ext='txt') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)


    def test_large_gzip_file(self):
        with gen_big_text_file(64 << 10, 5 << 20, ext='gz') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)

        with gen_big_text_file(1 << 20, 5 << 20, ext='gz') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)

    def test_large_bz2_file(self):
        with gen_big_text_file(64 << 10, 5 << 20, ext='bz2') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)

        with gen_big_text_file(1 << 20, 5 << 20, ext='bz2') as f:
            rd = self.sc.textFile(f.name, splitSize=512 * 1024)
            self.assertEqual(rd.count(), f.cnt)

    def test_binary_file(self):
        d = self.sc.makeRDD(list(range(100000)), 1)
        self.assertEqual(d.saveAsBinaryFile('/tmp/tout', fmt="I"),
            ['/tmp/tout/0000.bin'])
        rd = self.sc.binaryFile('/tmp/tout', fmt="I", splitSize=10<<10)
        self.assertEqual(rd.count(), 100000)
        shutil.rmtree('/tmp/tout')

    def test_table_file(self):
        N = 100000
        d = self.sc.makeRDD(list(zip(list(range(N)), list(range(N)))), 1)
        self.assertEqual(d.saveAsTableFile('/tmp/tout'), ['/tmp/tout/0000.tab',])
        rd = self.sc.tableFile('/tmp/tout', splitSize=64<<10)
        self.assertEqual(rd.count(), N)
        self.assertEqual(rd.map(lambda x:x[0]).reduce(lambda x,y:x+y), sum(range(N)))

        d.asTable(['f1', 'f2']).save('/tmp/tout')
        rd = self.sc.table('/tmp/tout')
        self.assertEqual(rd.map(lambda x:x.f1+x.f2).reduce(lambda x,y:x+y), 2*sum(range(N)))
        shutil.rmtree('/tmp/tout')

    def test_batch(self):
        from math import ceil
        d = list(range(1234))
        rdd = self.sc.makeRDD(d, 10).batch(100)
        self.assertEqual(rdd.flatMap(lambda x:x).collect(), d)
        self.assertEqual(rdd.filter(lambda x: len(x)<=2 or len(x) >100).collect(), [])

    def test_partial_file(self):
        p = 'tests/test_rdd.py'
        l = 300
        with open(p) as f:
            d = f.read(l+50)
        start = 100
        while d[start-1] != '\n':
            start += 1
        while d[l-1] != '\n':
            l += 1
        d = d[start:l-1]
        rdd = self.sc.partialTextFile(p, start, l, l)
        self.assertEqual('\n'.join(rdd.collect()), d)
        rdd = self.sc.partialTextFile(p, start, l, (l-start)//5)
        self.assertEqual('\n'.join(rdd.collect()), d)

    def test_beansdb(self):
        N = 100
        l = list(range(N))
        d = list(zip(list(map(lambda x: str(x).encode('utf-8'), l)), l))
        num_splits = 10
        rdd = self.sc.makeRDD(d, num_splits)
        root = '/tmp/beansdb'

        def newpath(c):
            return  os.path.join(root, str(c))

        def check_rdd(_rdd, files, num_w, num_r):
            self.assertEqual(files,
                ['%s/%03d.data' % (path, i) for i in range(num_w)])
            self.assertEqual(len(_rdd), num_r)
            self.assertEqual(_rdd.count(), N)
            self.assertEqual(sorted(_rdd.map(lambda k_v:(k_v[0],k_v[1][0])).collect()), sorted(d))
            s = _rdd.map(lambda x:x[1][0]).reduce(lambda x,y:x+y)
            self.assertEqual(s, sum(l))

        path = newpath(0)
        files = rdd.saveAsBeansdb(path)
        rdd = self.sc.beansdb(path, depth=0, filter=lambda x: x!="")
        check_rdd(rdd, files, num_splits, num_splits)

        path = newpath(1)
        files = rdd.saveAsBeansdb(path, valueWithMeta=True)
        rdd = self.sc.beansdb(path, depth=0, fullscan=True, only_latest=True)
        num_splits_reduce = int(ceil(num_splits/4))
        check_rdd(rdd, files, num_splits, num_splits_reduce)

        path = newpath(num_splits_reduce)
        files = rdd.map(lambda k_v1:(k_v1[0],k_v1[1][0])).saveAsBeansdb(path)
        rdd = self.sc.beansdb(path, raw=True, depth=0, fullscan=True)
        rdd = rdd.mapValue(lambda v:(restore_value(*v[0]), v[1], v[2]))
        check_rdd(rdd, files, num_splits_reduce, num_splits_reduce)

        shutil.rmtree(root)

    def test_beansdb_invalid_key(self):
        func = is_valid_key
        input_expect = [
            (b'/test/aaa/12321', True),
            (b'a' * 251, False),
            (b'/a/b\n/c', False),
            (b'/a/b/\r/d', False),
            (b'/a/b/\0/e', False),
            (b'/a/b /c', False),
            (b'/a/b \n/d', False),
        ]
        for key, expect in input_expect:
            self.assertEqual(func(key), expect)

    def test_enumerations(self):
        N = 100
        p = 10
        l = list(range(N))
        d1 = [(x//p, x) for x in l]
        d2 = list(enumerate(l))
        rdd = self.sc.makeRDD(l, p)
        self.assertEqual(rdd.enumeratePartition().collect(), d1)
        self.assertEqual(rdd.enumerate().collect(), d2)

    def test_tabular(self):
        d = list(range(10000))
        d = list(zip(d, list(map(str, d)), list(map(float, d))))
        path = '/tmp/tabular-%s' % os.getpid()
        try:
            self.sc.makeRDD(d).saveAsTabular(path, 'f_int, f_str, f_float', indices=['f_str', 'f_float'])
            r = self.sc.tabular(path, fields=['f_float', 'f_str']).collect()
            for f, s in r:
                self.assertEqual(type(f), float)
                self.assertEqual(type(s), str)
                self.assertEqual(str(int(f)), s)
            self.assertEqual(sorted(x.f_float for x in r), sorted(x[2] for x in d))

            r = self.sc.tabular(path, fields='f_int f_float').filterByIndex(f_float=lambda x:hash(x) % 2).collect()
            for i, f in r:
                self.assertEqual(type(i), int)
                self.assertEqual(type(f), float)
                self.assertEqual(i, int(f))
                self.assertTrue(hash(f) % 2)

            self.assertEqual(sorted(x.f_int for x in r), sorted(x[0] for x in d if hash(x[2]) %2))
        finally:
            try:
                shutil.rmtree(path)
            except OSError:
                pass

    def test_iter(self):
        d = list(range(1000))
        rdd = self.sc.makeRDD(d, 10)
        assert d == [i for i in rdd]

    def test_checkpoint(self):
        checkpoint_path = mkdtemp()
        try:
            d = list(range(1000))
            rdd = self.sc.makeRDD(d, 15).map(lambda x: x+1).checkpoint(checkpoint_path)
            assert rdd._dependencies
            r = rdd.collect()
            assert not rdd._dependencies
            self.assertEqual(len(rdd), 15)
            self.assertEqual(rdd.collect(), r)
        finally:
            shutil.rmtree(checkpoint_path)

    def test_checkpoint_partial(self):
        checkpoint_path = mkdtemp()
        try:
            d = list(range(1000))
            r = list(range(1, 1001))
            rdd = self.sc.makeRDD(d, 15).map(lambda x: x+1).checkpoint(checkpoint_path)
            assert rdd._dependencies
            sum(self.sc.runJob(rdd, lambda x: list(x), [0]), [])
            assert not rdd._dependencies
            self.assertEqual(len(rdd), 15)
            self.assertEqual(rdd.collect(), r)
        finally:
            shutil.rmtree(checkpoint_path)

    def test_long_lineage(self):
        checkpoint_path = mkdtemp()
        try:
            d = list(range(1000))
            rdd = self.sc.makeRDD(d, 15)
            for i in range(10):
                for j in range(100):
                    rdd = rdd.map(lambda x: x+1)
                rdd.checkpoint(checkpoint_path)
                r = rdd.collect()
                self.assertEqual(r, [x + 100 for x in d])
                d = r
        finally:
            shutil.rmtree(checkpoint_path)

    def test_long_recursion(self):
        d = list(range(10))
        rdd = self.sc.makeRDD(d)
        for i in range(1000):
            rdd = rdd.map(lambda x: x+1)

        loads(dumps(rdd))
        self.assertEqual(rdd.collect(), [x+1000 for x in d])

    def test_cache_shuffle(self):
        rdd1 = self.sc.parallelize([(1, 11), (2, 12), (3, 22)]).cache()
        rdd2 = self.sc.parallelize([(1, 33), (2, 44), (4, 55)]).cache()
        expected = set([(2, (12, 44)), (1, (11, 33))])
        self.assertEqual(set(rdd1.join(rdd2).collect()), expected)
        self.assertEqual(set(rdd1.join(rdd2).collect()), expected)

#class TestRDDInProcess(TestRDD):
#    def setUp(self):
#        self.sc = DparkContext("process")


if __name__ == "__main__":
    unittest.main()
