import argparse
import json
import operator
import os
import platform
from enum import Enum
from functools import reduce
from hashlib import md5
from itertools import chain, filterfalse, islice
from typing import List, Union

import lz4.block
import msgpack
from colorama import Fore, Style, init
from msgpack import ExtraData, ExtType, Timestamp
from msgpack.msgint import *
from py3rijndael import Pkcs7Padding, RijndaelCbc

# Generated using System.Security.Cryptography.Rfc2898DeriveBytes.GetBytes()
# Password: "frAQBc8Wsa1xVPfv"
# Salt:     "JcrgRYwTiizs2trQ"
RIJNDAEL_KEY = b'\x00\x6c\x01\x6a\xae\x7a\x12\x58\xd0\x4d\x70\xa1\x9e\x72\x28\x69'
RIJNDAEL_IV = b'\x98\x06\xd3\x73\x1e\x56\xbd\xc7\x68\x5d\x25\xd6\xa1\x03\x49\x3f'


def new_rijndael():
    return RijndaelCbc(RIJNDAEL_KEY, RIJNDAEL_IV, Pkcs7Padding(16), 16)


def unpack_skip_extra(data):
    try:
        return msgpack.unpackb(data)
    except ExtraData as e:
        return e.unpacked


class SaveDataChunkType(Enum):
    VERSION = 0
    BYTES = 1
    MENU = 2
    PADDING = 3


def decrypt_save(data: bytes):
    decrypted = []

    offset = 0
    while offset + 0x30 <= len(data):
        chunk_header = new_rijndael().decrypt(data[offset: offset + 0x30])
        offset += 0x30

        unpacked_header = unpack_skip_extra(chunk_header)

        if len(unpacked_header) != 3:
            raise Exception('Unexpected save chunk header format.')

        chunk_type, chunk_size, chunk_hash = unpacked_header

        chunk_type = SaveDataChunkType(chunk_type)

        if chunk_type == SaveDataChunkType.PADDING:
            decrypted.append(DuplicateDict([('__data_type__', chunk_type.name), ('__padding_size__', len(data))]))
        else:
            chunk = new_rijndael().decrypt(data[offset: offset + chunk_size])

            unpacked_chunk = unpack_skip_extra(chunk)

            if len(unpacked_chunk) and isinstance(unpacked_chunk[0], bytes):
                unpacked_chunk[0] = unpack_msg(unpacked_chunk[0])

            decrypted.append(DuplicateDict([('__data_type__', chunk_type.name), ('__data__', unpacked_chunk)]))

        offset += chunk_size

    return decrypted


def encrypt_save(entries: list):
    encrypted = b''

    for entry in entries:
        if '__data_type__' not in entry:
            raise Exception('Invalid save data json.')

        chunk_type = SaveDataChunkType[entry['__data_type__']]

        # If chunk type is PADDING, just add padding to the specified size
        if chunk_type == SaveDataChunkType.PADDING:
            pad_size = entry['__padding_size__']
            chunk_hash = '0' * 32
            chunk = b'\x00' * (pad_size - (len(encrypted) + 0x30))
        else:
            data = entry['__data__']
            packed = repack_msg(data)

            # Hash should be calculated before padding
            chunk_hash = md5(packed).hexdigest()

            chunk = new_rijndael().encrypt(packed)

        chunk_size = len(chunk)
        chunk_header = repack_msg([chunk_type, chunk_size, chunk_hash])

        encrypted += new_rijndael().encrypt(chunk_header) + chunk

    return encrypted


class HashableList(list):
    def __hash__(self) -> int:
        return len(self).__hash__()


class DuplicateDict(dict):
    """A `dict` that allows duplicate keys.\n
    Not very functional outside of this program's usage.
    """

    _list: List[tuple]

    def __init__(self, *args, **kwargs):
        self._list = list()
        self.update(*args, **kwargs)

    def update(self, *args, **kwargs):
        for k, v in chain(*args):
            self[k] = v
        for k, v in kwargs.items():
            self[k] = v

    def __hash__(self) -> int:
        return len(self).__hash__()

    def __setitem__(self, key, value):
        self._list.append((key, value))

    def set_tuple(self, index, value):
        self._list[index] = value

    def get_tuple(self, index):
        return self._list[index]

    def __getitem__(self, key):
        # return next(chain(filter(lambda x: x[0] == key, self._list), [(None, None)]))[1]
        return next(chain([item[1] for item in self._list if item[0] == key], [None]))

    def __len__(self) -> int:
        return len(self._list)

    def __contains__(self, key) -> bool:
        # This is sufficient for the purposes of this program
        # return o in list(chain(*self._list))[::2]
        return any([True for item in self._list if item[0] == key])

    def items(self):
        return iter(self._list)

    def items_list(self):
        # Compatiblity method for DuplicateDictJson
        return self.items()


class DuplicateDictJson(DuplicateDict):
    def needs_pairs(self):
        return any([True for item in self._list if not isinstance(item[0], (str, int, float))])

    def items(self):
        result = super().items()

        if self.needs_pairs():
            result = chain([('__keyvaluepairs__', True)], chain(
                *map(lambda x: ((f'key_{x[0]}', x[1][0]), (f'val_{x[0]}', x[1][1])), enumerate(result))))

        return result

    def items_list(self):
        return super().items()


def duplicate_dict_hook(pairs):
    """Builds DuplicateDict from msgpack"""

    return DuplicateDictJson(pairs)


def duplicate_dict_hook_json(pairs):
    """Builds DuplicateDict from json"""

    try:
        # Here, we only need to check for lists because other dicts have already been cast to DuplicateDict
        if pairs[0][0] == '__keyvaluepairs__':
            iterx = map(lambda x: x[1], islice(pairs, 1, None, 1))
            pairs = map(lambda x: (HashableList(x[0]) if isinstance(x[0], list) else x[0], x[1]), zip(iterx, iterx))
    except:
        pass

    return object_hook_json(DuplicateDict(pairs))


def default_hook_json(obj):
    if isinstance(obj, Timestamp):
        return DuplicateDictJson([('__msgpack_timestamp__', True), ('unix_nano', obj.to_unix_nano())])
    return obj


def object_hook_json(obj):
    if '__msgpack_timestamp__' in obj:
        obj = Timestamp.from_unix_nano(obj['unix_nano'])
    return obj


class ExtTypeBase:
    pass


class ExtBufferSizes(ExtTypeBase):
    _code: int = 0x62   # = 98 = 'b'
    sizes: List[int]

    def __init__(self, sizes: List[int]):
        self.sizes = sizes

    def __len__(self):
        return len(self.sizes)

    def __iter__(self):
        return iter(self.sizes)

    def __getitem__(self, item):
        return self.sizes[item]


def default_hook(obj):
    if isinstance(obj, ExtBufferSizes):
        return msgpack.ExtType(obj._code, reduce(operator.add, map(msgpack.packb, obj.sizes)))
    if isinstance(obj, SaveDataChunkType):
        return obj.value
    raise TypeError("Unknown type: %r" % (obj,))


def dupe_dict_to_json(entries: DuplicateDictJson):
    """Converts non-str keys to strings while storing their type."""

    if isinstance(entries, (list, tuple)):
        list(map(dupe_dict_to_json, entries))
    elif isinstance(entries, DuplicateDict):
        for i, (k, v) in enumerate(entries.items_list()):
            dupe_dict_to_json(v)

            # if not isinstance(k, str):
            if type(k).__name__ in KEY_TYPES:
                entries.set_tuple(i, (f'keytype_{type(k).__name__}_{k}', v))


def dupe_dict_to_json_schema(entries: DuplicateDictJson):
    """Converts non-str keys to strings while storing their type."""

    if isinstance(entries, list):
        for i, item in filter(lambda x: isinstance(x[1], bytes), enumerate(entries)):
            try:
                entries[i] = unpack_msg(item)
            except:
                pass
        return HashableList(map(dupe_dict_to_json_schema, entries))
    # elif isinstance(entries, tuple):
    #     return tuple(map(dupe_dict_to_json_schema, entries))
    elif isinstance(entries, DuplicateDict):
        schema_entries = DuplicateDictJson()

        for i, (k, v) in enumerate(entries.items_list()):
            # if not isinstance(k, str):
            if type(k).__name__ in KEY_TYPES:
                k = f'keytype_{type(k).__name__}_{k}'

            if isinstance(v, bytes):
                try:
                    v = unpack_msg(v)
                except:
                    pass

            entries.set_tuple(i, (k, v))
            schema_entries[dupe_dict_to_json_schema(k)] = dupe_dict_to_json_schema(v)

        return schema_entries
    elif isinstance(entries, Timestamp):
        # Needs to be done for the schema to align properly with the json
        return dupe_dict_to_json_schema(default_hook_json(entries))

    return type(entries).__name__


def json_dump(entries: dict, file, path, use_schema):
    if use_schema:
        schema = dupe_dict_to_json_schema(entries)
        with open(path[:-5] + '.msgschema.json', 'w', encoding='utf-8') as f:
            json.dump(schema, f, ensure_ascii=False, indent=2, skipkeys=False)
    else:
        dupe_dict_to_json(entries)

    json.dump(entries, file, ensure_ascii=False, indent=2, default=default_hook_json, skipkeys=False)


KEY_TYPES = {
    'int': int,
    'float': float,
    'msgIntBase': msgIntBase,
    'msgUInt8': msgUInt8,
    'msgInt8': msgInt8,
    'msgUInt16': msgUInt16,
    'msgInt16': msgInt16,
    'msgUInt32': msgUInt32,
    'msgInt32': msgInt32,
    'msgUInt64': msgUInt64,
    'msgInt64': msgInt64,
    'msgUByte': msgUByte,
    'msgByte': msgByte,
    'msgFloat': msgFloat,
    'msgDouble': msgDouble,
}


def json_to_dupe_dict(entries: DuplicateDict):
    """Converts keys back to their original type."""

    if isinstance(entries, (list, tuple)):
        list(map(json_to_dupe_dict, entries))
    elif isinstance(entries, DuplicateDict):
        for i, (k, v) in enumerate(entries.items()):
            if isinstance(k, str) and k.startswith('keytype_'):
                _, t, x = k.split('_', 2)
                k = KEY_TYPES[t](x)

            json_to_dupe_dict(k)
            json_to_dupe_dict(v)

            entries.set_tuple(i, (k, v))


def json_to_dupe_dict_schema(entries: DuplicateDict, schema: DuplicateDict):
    """Converts keys back to their original type."""

    if isinstance(entries, list):
        entries = HashableList(map(json_to_dupe_dict_schema, entries, schema))
        if '__should_be_compressed__' in entries:
            entries = repack_msg(entries)
        return entries
    # elif isinstance(entries, tuple):
    #     return tuple(map(json_to_dupe_dict_schema, entries, schema))
    elif isinstance(entries, DuplicateDict):
        for i, ((k1, v1), (k2, v2)) in enumerate(zip(entries.items(), schema.items())):
            k1 = json_to_dupe_dict_schema(k1, k2)
            v1 = json_to_dupe_dict_schema(v1, v2)

            if isinstance(k1, str) and k1.startswith('keytype_'):
                _, t, x = k1.split('_', 2)
                k1 = KEY_TYPES[t](x)

            entries.set_tuple(i, (k1, v1))

        return entries

    if typ := KEY_TYPES.get(schema):
        return typ(entries)

    return entries


def json_load(file, path, use_schema):
    entries = json.load(file, object_pairs_hook=duplicate_dict_hook_json)

    if use_schema:
        schema_path = path[:-5] + '.msgschema.json'
        if not os.path.isfile(schema_path):
            raise Exception(f'Cannot find schema file: {schema_path}')

        with open(schema_path, encoding='utf-8') as f:
            schema = json.load(f, object_pairs_hook=duplicate_dict_hook_json)
        entries = json_to_dupe_dict_schema(entries, schema)
    else:
        json_to_dupe_dict(entries)

    return entries


def decompress_msg_list(items):
    try:
        if isinstance(items[0], ExtBufferSizes):
            decompressed = b''
            sizes: ExtBufferSizes = items[0]

            for size, buffer in zip(sizes, items[1:]):
                if not isinstance(buffer, bytes):
                    raise Exception('Unknown file structure - Unable to decompress')

                decompressed += lz4.block.decompress(buffer, uncompressed_size=size)

            items = ['__should_be_compressed__', unpack_msg(decompressed)]
    except:
        pass

    return HashableList(items)


def unpack_extra(data, ext_hook=ExtType, strict_map_key=False):
    """Unpacks using msgpack continuously until no extra data exists."""

    unpacked = []

    while True:
        try:
            unpacked.append(msgpack.unpackb(data, ext_hook=ext_hook, object_pairs_hook=duplicate_dict_hook,
                                            list_hook=decompress_msg_list, strict_map_key=strict_map_key))
            break
        except ExtraData as e:
            unpacked.append(e.unpacked)
            data = e.extra

    return unpacked


def unpack_hook(code, data):
    if code == 0x62:
        return ExtBufferSizes(unpack_extra(data))

    return ExtType(code, data)


def unpack_msg(data: bytes) -> Union[dict, list]:
    unpacked = unpack_extra(data, ext_hook=unpack_hook)
    compressed_indices = [i for i, x in enumerate(unpacked) if isinstance(x, list) and '__should_be_compressed__' in x]

    if len(unpacked) != 1 and len(compressed_indices):
        unpacked = ['__compressed_data_indices__', compressed_indices] + unpacked

    return unpacked[0] if len(unpacked) == 1 else (['__has_extra_data__'] + unpacked)


def pack_extra(entries, default=default_hook):
    return msgpack.packb(entries, default=default)


def repack_msg(entries: Union[dict, list]) -> bytes:
    if isinstance(entries, list):
        if '__has_extra_data__' in entries:
            entries.remove('__has_extra_data__')
            if '__compressed_data_indices__' in entries:
                entries.remove('__compressed_data_indices__')
                indices = entries.pop(0)

                for i, entry in filterfalse(lambda x: x[0] in indices and isinstance(x[1], bytes), enumerate(entries)):
                    entries[i] = repack_msg(entry)
            else:
                entries = map(repack_msg, entries)

            return reduce(operator.add, entries, b'')
        elif '__should_be_compressed__' in entries:
            if len(entries) != 2:
                raise Exception('Unexpected structure for compression.')

            packed = repack_msg(entries[1])

            sizes = []
            packed_arr = []

            CHUNK_SIZE = 32_767
            for i in range((len(packed) // CHUNK_SIZE) + 1):
                chunk = packed[i*CHUNK_SIZE:(i+1)*CHUNK_SIZE]
                sizes.append(len(chunk))
                packed_arr.append(lz4.block.compress(chunk, store_size=False))

            return repack_msg([ExtBufferSizes(sizes)] + packed_arr)

    return msgpack.packb(entries, default=default_hook)


def pause_win():
    print()
    os.system('pause')


def pause_other():
    input('\nPress any key to continue . . . ')


# Make sure terminal colors appear nicely on windows cmd + powershell
if platform.system() == "Windows":
    init(convert=None)
    pause = pause_win
else:
    pause = pause_other


def colorize(text, color):
    return f'{color}{text}{Style.RESET_ALL}'


def colorize_red(text):
    return colorize(text, Fore.RED)


def colorize_green(text):
    return colorize(text, Fore.GREEN)


def colorize_blue(text):
    return colorize(text, Fore.BLUE)


VERSION = 'v1.1'
AUTHOR = 'SutandoTsukai181, original script by Arsym'

mode_help = """\nMode: If neither \"--unpack\" nor \"--repack\" are specified, then both are enabled at the same time (i.e. json files will be repacked, non-json files will be unpacked).\n
Schema: If \"--use-schema\" is specified, then:
    for each file unpacked, another file with extension \".msgschema.json\" will be created, which will contain type information for the unpacked json file.
    for each file repacked, if its schema exists, it will be used when repacking to get the correct type information.
This should be used to enforce specific packing types to fix repacking for some files (i.e. some .code files).
"""


class ArgumentParser2(argparse.ArgumentParser):
    def format_help(self):
        return super().format_help() + mode_help

    def error(self, message):
        self.print_help()
        pause()
        self.exit(2, f'\n{self.prog}: error: {message}\n')


def main():
    print(colorize_blue(f'ai2_msgpack {VERSION}'))
    print(colorize_blue(f'By {AUTHOR}\n'))

    parser = ArgumentParser2(
        description='Unpacks/repacks MessagePack files from Ai: The Somnium Files: nirvanA Initiative.')

    parser.add_argument('input', nargs='+', action='store',
                        help='path(s) to input file(s) and/or folder(s) that contain files to process.')
    parser.add_argument('-o', '--output', action='store',
                        help='path to output directory (defaults to same directory as the input). If multiple input folders are given, this will be ignored for them.')

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument('-u', '--unpack', action='store_true',
                            help='Unpack non-json files into json, and ignore json files.')
    mode_group.add_argument('-r', '--repack', action='store_true',
                            help='Repack json files into MessagePack, and ignore non-json files.')

    parser.add_argument('-c', '--use-schema', dest='use_schema', action='store_true',
                        help='Write a schema along with unpacked files, and use an existing schema when repacking.')
    parser.add_argument('-d', '--save-data', dest='save_data', action='store_true',
                        help='Decrypt/encrypt save data files before unpacking/repacking. Enabling this means that all input files are save data files. This also enables \"--use-schema.\"')

    parser.add_argument('-a', '--overwrite-all', dest='overwrite', action='store_true',
                        help='Overwrite existing files without prompting.')
    parser.add_argument('-s', '--silent', action='store_true',
                        help='Remove all prompts during execution. Enabling this will enable \"--overwrite-all\".')

    args = parser.parse_args()
    result = args.silent

    folder_count = 0
    input_files = []
    for input_path in args.input:
        if os.path.isfile(input_path):
            input_files.append((False, input_path))
        elif os.path.isdir(input_path):
            folder_count += 1
            for path in os.listdir(input_path):
                if os.path.isfile(file_path := os.path.join(input_path, path)):
                    input_files.append((True, file_path))
        else:
            print(colorize_red(f'Skipping input path because it does not exist: \"{input_path}\"\n'))

    if len(input_files) == 0:
        print(f'{colorize_red("No files given exist.")}\nAborting.')
        return result

    use_output = folder_count <= 1
    if args.output and not use_output and not args.silent:
        print(colorize_blue('Multiple input folders detected. Output for these folders will be in the same folder, *not* the output folder.'))
        ans = input('Continue (Y/N)? ').lower()
        if ans != 'y':
            print('\nAborting.')
            return result

    if args.output and not os.path.isdir(args.output):
        args.output = os.path.realpath(args.output)
        os.makedirs(args.output, exist_ok=True)

    if args.silent:
        args.overwrite = True

    if args.save_data:
        args.use_schema = True

    unpack_func = decrypt_save if args.save_data else unpack_msg
    repack_func = encrypt_save if args.save_data else repack_msg

    for from_folder, file in input_files:
        new_path = ''
        print(f'Reading \"{file}\"...')

        try:
            if file.endswith('.json') and not file.endswith('.msgschema.json') and not args.unpack:
                # Repack
                with open(file, 'r', encoding='utf-8') as f:
                    msg = json_load(f, file, args.use_schema)

                new_path = os.path.join(args.output if (args.output and (use_output or not from_folder)) else os.path.dirname(
                    file), os.path.basename(file[:-5] if file.endswith('.json') else file))

                if os.path.isfile(new_path) and not args.overwrite:
                    print(colorize_blue(f'Output file already exists: \"{new_path}\"\n'))
                    ans = input('Overwrite (Y/N/A)? ').lower()

                    if ans == 'a':
                        args.overwrite = True
                    elif ans != 'y':
                        print(colorize_blue(f'Skipped \"{new_path}\"\n'))
                        continue

                msg = repack_func(msg)
                with open(new_path, 'wb') as f:
                    f.write(msg)

                print(colorize_green(f'Repacked \"{new_path}\"!\n'))
            elif not file.endswith('.json') and not args.repack:
                # Unpack
                with open(file, 'rb') as f:
                    msg = f.read()
                msg = unpack_func(msg)

                new_path = os.path.join(args.output if (args.output and (use_output or not from_folder)) else os.path.dirname(file),
                                        os.path.basename(file) + '.json')

                if os.path.isfile(new_path) and not args.overwrite:
                    print(colorize_blue(f'Output file already exists: \"{new_path}\"\n'))
                    ans = input('Overwrite (Y/N/A)? ').lower()

                    if ans == 'a':
                        args.overwrite = True
                    elif ans != 'y':
                        print(colorize_blue(f'Skipped \"{new_path}\"\n'))
                        continue

                with open(new_path, 'w', encoding='utf-8') as f:
                    json_dump(msg, f, new_path, args.use_schema)

                print(colorize_green(f'Unpacked \"{new_path}\"!\n'))
            else:
                print(colorize_blue(f'Skipped file because of mode: \"{file}\"\n'))
        except Exception as e:
            print(colorize_red(
                f'Error {"writing output" if new_path else "processing input"} file: \"{file}\"') + f'\nError details: {e}\n')

    print(colorize_blue('Finished processing all files.'))
    return result


if __name__ == '__main__':
    if main() is not True:
        pause()
