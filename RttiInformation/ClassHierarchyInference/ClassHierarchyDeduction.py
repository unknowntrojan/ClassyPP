import itertools

import networkx as nx
from networkx import DiGraph
from ...RttiInformation import ClassContext
from ...Common import Utils
from ... import Config
import binaryninja as bn
from typing import *


def GetNumberOfBaseClasses(bcd_addr: int) -> int:
    return int(ClassContext.base_class_descriptors[bcd_addr]["numContainedBases"], 16)


def GetClassDescriptorFromChd(chd_addr: int) -> int:
    return ClassContext.class_hierarchy_descriptors[chd_addr][1][0]


def MapAllVirtualFunctions() -> Dict[int, Dict[int, List[int]]]:
    # Go over all functions in all known vTables and map the function to all the classes
    # that use it in the vTable.
    # {function_address: {function_index_in_vtable: [col_address, ...]}
    mapped_functions: Dict[int, Dict[int, List[int]]] = dict()
    for col_addr, col_info in ClassContext.complete_object_locators.items():
        chd_addr: int = col_info[0]
        function_index: int = 0
        for function_addr in col_info[3]:
            Utils.LogToFile(f'Checking function: {function_addr}')
            if mapped_functions.get(function_addr):
                if mapped_functions[function_addr].get(function_index):
                    mapped_functions[function_addr][function_index].append(GetClassDescriptorFromChd(chd_addr))
                else:
                    Utils.LogToFile(f'MapAllVirtualFunctions: WARNING! function {function_addr} exist in different '
                                    f'indexes in different classes!')
                    mapped_functions[function_addr].update({function_index: [GetClassDescriptorFromChd(chd_addr)]})
            else:
                mapped_functions.update({function_addr: {function_index: [GetClassDescriptorFromChd(chd_addr)]}})
            function_index += 1
    return mapped_functions


def GetLowestCommonAncestor(common_classes: List[int], class_hierarchy_graph: DiGraph) -> int:
    current_set = set(common_classes)
    safety_measure: int = 20

    while len(current_set) > 1 and safety_measure != 0:
        iter_list = list(itertools.combinations(current_set, 2))
        lca_list = list(nx.all_pairs_lowest_common_ancestor(class_hierarchy_graph, iter_list))
        current_set = set()
        for pair, lowest_ancestor in lca_list:
            current_set.add(lowest_ancestor)
        safety_measure -= 1

    if len(current_set) > 0:
        Utils.LogToFile(f'GetLowestCommonAncestor: current_set - {current_set})')
        return current_set.pop()
    else:
        Utils.LogToFile(f'GetLowestCommonAncestor: No LCA found for function common_classes - {common_classes})')
        return 0


def FuncNameNotDefinedByPDB(func: bn.Function) -> bool:
    # If a PDB database has already defined this function name then we do not want to override
    # that name since it is a more accurate description of the function.
    # instead, we just add a comment in the function to indicate it is part of a vTable.
    return func.name.startswith("sub_") or "::" not in func.name


def RenameFunction(bv: bn.BinaryView, vtable_function: int, lca: int, function_index: int) -> bool:
    if vtable_function > bv.end or vtable_function < bv.start:
        print(f"function at {vtable_function:x} is outside of our binary!")
        return False
    
    class_name: str = ClassContext.base_class_descriptors[lca]['class_name'] # .replace("class ", "")
    try:
        func: bn.Function = bv.get_function_at(vtable_function)
        new_func_name =  f'{class_name}::Method{function_index:03}'
        if not func:
            func = bv.create_user_function(vtable_function)
            print(f'Defined new function at {hex(vtable_function)}')
            bv.update_analysis_and_wait()
        if FuncNameNotDefinedByPDB(func) or "::Method" in func.name:
            func.name = new_func_name
        else:
            func.set_comment_at(func.start,new_func_name)
        return True
    except Exception as e:
        print(f"Unable to rename function {hex(vtable_function)}, got Exception: \n{e}")
        return False


def DefinevTableFunctions(bv: bn.BinaryView, class_hierarchy_graph: DiGraph):
    mapped_functions: Dict[int: List[int]] = MapAllVirtualFunctions()

    print("finding ambiguous vfuncs...")
    
    ambiguous_functions = list()
    
    for vfunc, info in mapped_functions.items():
        indices = dict()
        for idx, class_list in info.items():
            if idx in indices:
                indices[idx].extend(class_list)
            else:
                indices[idx] = list()
                indices[idx].extend(class_list)
                        
        if len(indices) > 1:
            def classes_for_vfunc(index: int, classes: list[int]):
                return { ClassContext.base_class_descriptors[class_index]['class_name'] for class_index in classes }
                # (index, map(lambda class_index: ClassContext.base_class_descriptors[class_index]['class_name'], classes))
            
            print("ambiguous function at {vfunc:x} is present at different indices in several vtables: {present_in}!".format(vfunc=vfunc, present_in={k: classes_for_vfunc(k, v) for k,v in indices.items()}))
            ambiguous_functions.append(vfunc)

    Utils.LogToFile(f'mapped_functions: {mapped_functions}')
    for vtable_function, info in mapped_functions.items():
        if vtable_function in ambiguous_functions:
            continue
        
        for function_index, class_list in info.items():
            if len(class_list) > 1:
                lca: int = GetLowestCommonAncestor(class_list, class_hierarchy_graph)
            elif len(class_list) == 1:
                lca: int = class_list[0]
            else:
                Utils.LogToFile(f'DefinevTableFunctions: Got 0 members in function {vtable_function} class list')
                lca: int = 0
            Utils.LogToFile(f'DefinevTableFunctions: function {vtable_function} lca is {str(lca)}')

            if lca != 0 and not RenameFunction(bv, vtable_function, lca, function_index):
                Utils.LogToFile(f'DefinevTableFunctions: ERROR! Failed to rename function {vtable_function} '
                                f'with lca {str(lca)}')


def CreateAllBaseTypeNodes(class_hierarchy_graph: DiGraph) -> bool:
    for bcd_addr, bcd_info in ClassContext.base_class_descriptors.items():
        class_hierarchy_graph.add_node(bcd_addr)
        nx.set_node_attributes(class_hierarchy_graph, {bcd_addr: bcd_info})
        Utils.LogToFile(f'Added node {bcd_addr}')
    return True


def GetBaseClassArrayFromBcd(bcd_info: dict):
    return ClassContext.class_hierarchy_descriptors[int(bcd_info['pClassDescriptor'], 16)][1]


def CreateBcdHierarchyRecursively(base_class_array: List[int],
                                  resolved_bcd: List[int],
                                  class_hierarchy_graph: DiGraph) -> int:
    i = 1
    Utils.LogToFile(str(class_hierarchy_graph.edges))
    while i < len(base_class_array):
        Utils.LogToFile(f'CreateBcdHierarchyRecursively: array_index {i}, base_class_array: {base_class_array}')
        Utils.LogToFile(f'CreateBcdHierarchyRecursively: Adding edge '
                        f'{base_class_array[i]} -> {base_class_array[0]}')
        class_hierarchy_graph.add_edge(base_class_array[i], base_class_array[0])

        next_i = GetNumberOfBaseClasses(base_class_array[i]) + i + 1
        if next_i > 0:
            i += CreateBcdHierarchyRecursively(base_class_array[i:next_i],
                                               resolved_bcd,
                                               class_hierarchy_graph)
        else:
            i += 1
    return i


def WriteGraphToFile(graph: DiGraph, gexf=True, graphml=False):
    if gexf:
        # To read the following stored graph: read_gexf(Config.GRAPH_FILE_FULL_PATH)
        nx.write_gexf(graph, Config.GRAPH_FILE_FULL_PATH + 'RttiInformation.gexf')

    if graphml:
        # Write the graph in graphml form in order to be able to upload it to other databases (such as neo4j)
        # In neo4j:
        #           CALL apoc.import.graphml('RttiInformation.graphml', {storeNodeIds: true})
        #           MATCH (n)
        #           CALL apoc.create.addLabels([id(n)], [n.id])
        #           yield node
        #           return node
        nx.write_graphml(graph, Config.GRAPH_FILE_FULL_PATH + 'RttiInformation.graphml')


def CreateHierarchyGraph() -> nx.DiGraph:
    class_hierarchy_graph: DiGraph = nx.DiGraph()
    resolved_bcd: List[int] = list()
    if CreateAllBaseTypeNodes(class_hierarchy_graph):
        # print(ClassContext.base_class_descriptors)
        for bcd_addr, bcd_info in ClassContext.base_class_descriptors.items():
            CreateBcdHierarchyRecursively(GetBaseClassArrayFromBcd(bcd_info), resolved_bcd, class_hierarchy_graph)

    WriteGraphToFile(class_hierarchy_graph)

    return class_hierarchy_graph


def DefineClassHierarchy(bv: bn.BinaryView):
    DefinevTableFunctions(bv, CreateHierarchyGraph())
