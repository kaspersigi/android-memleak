/* SPDX-License-Identifier: Apache-2.0 */
/* Exercise the actual patched upstream parser without BPF/root/device access. */
#define main memleak_tool_main
#include "memleak.c"
#undef main

int main(int argc, char **argv)
{
    const struct argp parser = {
        .options = argp_options,
        .parser = argp_parse_arg,
        .doc = argp_args_doc,
    };
    int ret = argp_parse(&parser, argc, argv, 0, NULL, NULL);
    if (ret)
        return 1;
    printf("object=%s\ndefault=%s\nage_ns=%ld\nprefix=%s\n",
           env.object, default_object, env.min_age_ns, env.symbols_prefix);
    return 0;
}
