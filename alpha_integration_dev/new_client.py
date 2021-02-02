#
# Copyright (c) 2021, Neptune Labs Sp. z o.o.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Remember to set environment values:
        # self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].pop('tag2_to_remove')
        # self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].pop('tag4_remove_non_existing')
* NEPTUNE_API_TOKEN
* NEPTUNE_PROJECT
"""
import sys
from datetime import datetime

from PIL import Image

import neptune.alpha as neptune
from alpha_integration_dev.common_client_code import ClientFeatures
from neptune.alpha.attributes.constants import (
    LOG_ATTRIBUTE_SPACE,
    PROPERTIES_ATTRIBUTE_SPACE,
    SYSTEM_TAGS_ATTRIBUTE_PATH,
)


class NewClientFeatures(ClientFeatures):
    def __init__(self):
        super().__init__()
        self.exp = neptune.init()

    def modify_tags(self):
        self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].add('tag1')
        self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].add(['tag2_to_remove', 'tag3'])
        self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].remove('tag2_to_remove')
        self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].remove('tag4_remove_non_existing')
        # del self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH]  # TODO: NPT-9222

        self.exp.sync()
        assert set(self.exp[SYSTEM_TAGS_ATTRIBUTE_PATH].get()) == {'tag1', 'tag3'}

    def modify_properties(self):
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop'] = 'some text'
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_number'] = 42
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}nested/prop'] = 42
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_to_del'] = 42
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_list'] = [1, 2, 3]
        with open(self.text_file_path, mode='r') as f:
            self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_IO'] = f
        self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_datetime'] = datetime.now()
        self.exp.sync()
        del self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_to_del']

        assert self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop'].get() == 'some text'
        assert self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_number'].get() == 42
        assert self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}nested/prop'].get() == 42
        prop_to_del_absent = False
        try:
            self.exp[f'{PROPERTIES_ATTRIBUTE_SPACE}prop_to_del'].get()
        except AttributeError:
            prop_to_del_absent = True
        assert prop_to_del_absent

    def log_std(self):
        print('stdout text1')
        print('stdout text2')
        print('stderr text1', file=sys.stderr)
        print('stderr text2', file=sys.stderr)

    def log_series(self):
        # floats
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m1'].log(1)
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m1'].log(2)
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m1'].log(3)
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m1'].log(2)
        self.exp[f'{LOG_ATTRIBUTE_SPACE}nested/m1'].log(1)

        # texts
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m2'].log('a')
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m2'].log('b')
        self.exp[f'{LOG_ATTRIBUTE_SPACE}m2'].log('c')

        # images
        im_frame = Image.open(self.img_path)
        g_img = neptune.types.Image(im_frame)
        self.exp[f'{LOG_ATTRIBUTE_SPACE}g_img'].log(g_img)

    def handle_files_and_images(self):
        """NPT-9207"""
        return

    def other(self):
        return

    def run(self):
        self.modify_tags()
        self.modify_properties()
        self.log_std()
        self.log_series()
        self.handle_files_and_images()

        self.other()


if __name__ == '__main__':
    NewClientFeatures().run()
